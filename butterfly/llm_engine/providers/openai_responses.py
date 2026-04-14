"""OpenAI Responses API provider.

Targets o-series, gpt-5, and gpt-5-codex reasoning models where the Responses
API is the recommended path (OpenAI's migration guide, April 2026). Reasoning
items are captured and re-echoed across turns so chain-of-thought is retained
(same pattern as CodexProvider).

For legacy gpt-4x / non-reasoning models, use ``OpenAIProvider`` (Chat
Completions) instead.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import TYPE_CHECKING, Any, Callable, ClassVar

from butterfly.core.provider import Provider
from butterfly.core.types import TokenUsage, ToolCall
from butterfly.llm_engine.providers._common import _parse_json_args

if TYPE_CHECKING:
    from butterfly.core.types import Message
    from butterfly.core.tool import Tool


_VALID_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}


class OpenAIResponsesProvider(Provider):
    """OpenAI provider using the Responses API (``client.responses.create``).

    Environment variables
    ---------------------
    OPENAI_API_KEY   – API key (fallback when *api_key* is None)
    OPENAI_BASE_URL  – Optional custom endpoint
    """

    _supports_thinking: ClassVar[bool] = True

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 8096,
        max_retries: int = 3,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise ImportError(
                "Install the openai package: pip install 'openai>=1.0.0'"
            ) from None

        client_kwargs: dict[str, Any] = {
            "api_key": api_key or os.environ.get("OPENAI_API_KEY"),
            "max_retries": max_retries,
        }
        resolved_base = base_url or os.environ.get("OPENAI_BASE_URL")
        if resolved_base:
            client_kwargs["base_url"] = resolved_base

        self._client = AsyncOpenAI(**client_kwargs)
        self.max_tokens = max_tokens
        self._conversation_id = str(uuid.uuid4())
        self._pending_reasoning: list[dict[str, Any]] = []

    def consume_extra_blocks(self) -> list[dict]:
        blocks = self._pending_reasoning
        self._pending_reasoning = []
        return blocks

    async def complete(
        self,
        messages: list["Message"],
        tools: list["Tool"],
        system_prompt: str,
        model: str,
        *,
        on_text_chunk: Callable[[str], None] | None = None,
        cache_system_prefix: str = "",
        cache_last_human_turn: bool = False,
        thinking: bool = False,
        thinking_budget: int = 8000,
        thinking_effort: str = "high",
    ) -> tuple[str, list[ToolCall], TokenUsage]:
        full_system = (
            (cache_system_prefix + "\n\n" + system_prompt).strip()
            if cache_system_prefix
            else system_prompt
        )
        effort = thinking_effort if thinking_effort in _VALID_EFFORTS else "high"

        kwargs: dict[str, Any] = {
            "model": model,
            "instructions": full_system,
            "input": _convert_messages(messages),
            "max_output_tokens": self.max_tokens,
            "store": False,
            "parallel_tool_calls": True,
            "prompt_cache_key": self._conversation_id,
        }
        if tools:
            kwargs["tools"] = [_tool_to_responses(t) for t in tools]
        if thinking:
            kwargs["reasoning"] = {"effort": effort, "summary": "auto"}
            kwargs["include"] = ["reasoning.encrypted_content"]

        if on_text_chunk is not None:
            return await self._stream(kwargs, on_text_chunk)
        return await self._non_stream(kwargs)

    # ------------------------------------------------------------------

    async def _non_stream(
        self, kwargs: dict[str, Any]
    ) -> tuple[str, list[ToolCall], TokenUsage]:
        response = await self._client.responses.create(**kwargs)
        return _parse_response_object(response, pending=self._pending_reasoning)

    async def _stream(
        self,
        kwargs: dict[str, Any],
        on_text_chunk: Callable[[str], None],
    ) -> tuple[str, list[ToolCall], TokenUsage]:
        text_parts: list[str] = []
        tc_map: dict[str, dict[str, str]] = {}
        current_tc_id: str | None = None
        reasoning_items: list[dict[str, Any]] = []
        usage = TokenUsage()

        async with self._client.responses.stream(**kwargs) as stream:
            async for event in stream:
                etype = getattr(event, "type", "") or ""

                if etype == "response.output_text.delta":
                    delta = getattr(event, "delta", "") or ""
                    if delta:
                        text_parts.append(delta)
                        on_text_chunk(delta)

                elif etype == "response.reasoning_text.delta":
                    delta = getattr(event, "delta", "") or ""
                    if delta:
                        on_text_chunk(delta)

                elif etype == "response.reasoning_summary_text.delta":
                    delta = getattr(event, "delta", "") or ""
                    if delta:
                        on_text_chunk(delta)

                elif etype == "response.output_item.added":
                    item = _event_item_as_dict(event)
                    if item.get("type") == "function_call":
                        call_id = item.get("call_id") or str(uuid.uuid4())
                        tc_map[call_id] = {"name": item.get("name", ""), "args": ""}
                        current_tc_id = call_id

                elif etype == "response.function_call_arguments.delta":
                    delta_call_id = getattr(event, "call_id", None) or current_tc_id
                    delta = getattr(event, "delta", "") or ""
                    if delta_call_id and delta_call_id in tc_map:
                        tc_map[delta_call_id]["args"] += delta

                elif etype == "response.output_item.done":
                    item = _event_item_as_dict(event)
                    itype = item.get("type", "")
                    if itype == "function_call":
                        call_id = item.get("call_id", "")
                        if call_id in tc_map:
                            tc_map[call_id]["args"] = item.get(
                                "arguments", tc_map[call_id]["args"]
                            )
                        current_tc_id = None
                    elif itype == "reasoning":
                        reasoning_items.append(_capture_reasoning(item))

            final = await stream.get_final_response()
            usage = _extract_usage_from_obj(getattr(final, "usage", None))

        self._pending_reasoning = reasoning_items
        text = "".join(text_parts)
        tool_calls = [
            ToolCall(id=call_id, name=tc["name"], input=_parse_json_args(tc["args"]))
            for call_id, tc in tc_map.items()
            if tc["name"]
        ]
        return text, tool_calls, usage


# ======================================================================
# Conversion helpers
# ======================================================================


def _tool_to_responses(tool: "Tool") -> dict[str, Any]:
    """Responses-API tool schema is flat (no inner ``function`` wrapper)."""
    api = tool.to_api_dict()
    return {
        "type": "function",
        "name": api["name"],
        "description": api.get("description", ""),
        "parameters": api.get("input_schema", {"type": "object", "properties": {}}),
        "strict": False,
    }


def _convert_messages(messages: list["Message"]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "user":
            item = _convert_user(msg)
            if item:
                result.append(item)
        elif msg.role == "assistant":
            result.extend(_convert_assistant(msg))
        elif msg.role == "tool":
            result.extend(_convert_tool_result(msg))
    return result


def _convert_user(msg: "Message") -> dict[str, Any] | None:
    content = msg.content
    if isinstance(content, str):
        return {"role": "user", "content": [{"type": "input_text", "text": content}]}
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append({"type": "input_text", "text": block.get("text", "")})
            elif isinstance(block, str):
                parts.append({"type": "input_text", "text": block})
        return {"role": "user", "content": parts} if parts else None
    return None


def _convert_assistant(msg: "Message") -> list[dict[str, Any]]:
    content = msg.content
    if isinstance(content, str):
        return [_assistant_message_item(content)] if content else []
    if not isinstance(content, list):
        return []

    result: list[dict[str, Any]] = []
    text_parts: list[str] = []

    def flush_text() -> None:
        nonlocal text_parts
        if text_parts:
            result.append(_assistant_message_item("".join(text_parts)))
            text_parts = []

    for block in content:
        if not isinstance(block, dict):
            text_parts.append(str(block))
            continue
        btype = block.get("type", "")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "reasoning":
            flush_text()
            item: dict[str, Any] = {
                "type": "reasoning",
                "id": block.get("id") or f"rs_{uuid.uuid4().hex[:24]}",
                "summary": block.get("summary", []),
            }
            if "encrypted_content" in block:
                item["encrypted_content"] = block["encrypted_content"]
            result.append(item)
        elif btype == "tool_use":
            flush_text()
            tool_id = block.get("id", str(uuid.uuid4()))
            result.append({
                "type": "function_call",
                "id": f"fc_{tool_id}",
                "call_id": tool_id,
                "name": block.get("name", ""),
                "arguments": json.dumps(block.get("input", {})),
            })

    flush_text()
    return result


def _assistant_message_item(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
        "status": "completed",
    }


def _convert_tool_result(msg: "Message") -> list[dict[str, Any]]:
    content = msg.content
    if not isinstance(content, list):
        return []
    result = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            tool_use_id = block.get("tool_use_id", "")
            inner = block.get("content", "")
            if isinstance(inner, list):
                text = " ".join(
                    b.get("text", "") for b in inner
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                text = str(inner)
            result.append({
                "type": "function_call_output",
                "call_id": tool_use_id,
                "output": text,
            })
    return result


def _event_item_as_dict(event: Any) -> dict[str, Any]:
    item = getattr(event, "item", None)
    if item is None:
        return {}
    if isinstance(item, dict):
        return item
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        return dump(exclude_none=True)
    return {k: v for k, v in vars(item).items() if not k.startswith("_")}


def _capture_reasoning(item: dict[str, Any]) -> dict[str, Any]:
    captured: dict[str, Any] = {
        "type": "reasoning",
        "id": item.get("id", ""),
        "summary": item.get("summary", []),
    }
    if "encrypted_content" in item:
        captured["encrypted_content"] = item["encrypted_content"]
    return captured


# ======================================================================
# Response parsing (non-streaming)
# ======================================================================


def _parse_response_object(
    response: Any, *, pending: list[dict[str, Any]]
) -> tuple[str, list[ToolCall], TokenUsage]:
    """Parse a Responses API non-streaming response object."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for item in getattr(response, "output", []) or []:
        idict = item if isinstance(item, dict) else _event_item_as_dict_from_obj(item)
        itype = idict.get("type", "")
        if itype == "message":
            for c in idict.get("content", []):
                if isinstance(c, dict) and c.get("type") == "output_text":
                    text_parts.append(c.get("text", ""))
        elif itype == "function_call":
            tool_calls.append(ToolCall(
                id=idict.get("call_id", ""),
                name=idict.get("name", ""),
                input=_parse_json_args(idict.get("arguments", "") or ""),
            ))
        elif itype == "reasoning":
            pending.append(_capture_reasoning(idict))

    usage = _extract_usage_from_obj(getattr(response, "usage", None))
    return "".join(text_parts), tool_calls, usage


def _event_item_as_dict_from_obj(obj: Any) -> dict[str, Any]:
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump(exclude_none=True)
    return {k: v for k, v in vars(obj).items() if not k.startswith("_")}


def _extract_usage_from_obj(usage: Any) -> TokenUsage:
    if usage is None:
        return TokenUsage()
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    input_details = getattr(usage, "input_tokens_details", None)
    cached = getattr(input_details, "cached_tokens", 0) if input_details else 0
    cached = cached or 0
    output_details = getattr(usage, "output_tokens_details", None)
    reasoning = getattr(output_details, "reasoning_tokens", 0) if output_details else 0
    reasoning = reasoning or 0
    return TokenUsage(
        input_tokens=max(input_tokens - cached, 0),
        output_tokens=output_tokens,
        cache_read_tokens=cached,
        cache_write_tokens=0,
        reasoning_tokens=reasoning,
    )
