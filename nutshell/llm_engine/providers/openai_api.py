from __future__ import annotations
import json
import os
from typing import TYPE_CHECKING, Any, Callable, ClassVar

from nutshell.core.provider import Provider
from nutshell.core.types import TokenUsage, ToolCall
from nutshell.llm_engine.providers._common import _parse_json_args

if TYPE_CHECKING:
    from nutshell.core.types import Message
    from nutshell.core.tool import Tool


class OpenAIProvider(Provider):
    """LLM provider backed by OpenAI (GPT models).

    Supports the official ``openai`` Python SDK.  Works with standard API keys
    as well as OAuth tokens (e.g. from *openai-codex*).

    Environment variables
    ---------------------
    OPENAI_API_KEY   – API key or OAuth token (fallback when *api_key* is None)
    OPENAI_BASE_URL  – Optional custom endpoint
    """

    _supports_thinking: ClassVar[bool] = False

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 8096,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise ImportError(
                "Install the openai package: pip install 'openai>=1.0.0'"
            ) from None

        resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
        resolved_base = base_url or os.environ.get("OPENAI_BASE_URL") or None

        client_kwargs: dict[str, Any] = {"api_key": resolved_key}
        if resolved_base is not None:
            client_kwargs["base_url"] = resolved_base

        self._client = AsyncOpenAI(**client_kwargs)
        self.max_tokens = max_tokens

    # ------------------------------------------------------------------
    # Provider interface
    # ------------------------------------------------------------------

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
        thinking_effort: str = "high",  # ignored — OpenAI Chat Completions has no reasoning toggle
    ) -> tuple[str, list[ToolCall], TokenUsage]:
        api_messages = _build_messages(system_prompt, messages, cache_system_prefix)
        api_tools = [_tool_to_openai(t) for t in tools] if tools else []

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": api_messages,
        }
        if api_tools:
            kwargs["tools"] = api_tools

        if on_text_chunk is not None:
            return await self._stream_complete(kwargs, on_text_chunk)
        return await self._non_stream_complete(kwargs)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _non_stream_complete(
        self, kwargs: dict[str, Any]
    ) -> tuple[str, list[ToolCall], TokenUsage]:
        response = await self._client.chat.completions.create(**kwargs)
        return _parse_response(response)

    async def _stream_complete(
        self,
        kwargs: dict[str, Any],
        on_text_chunk: Callable[[str], None],
    ) -> tuple[str, list[ToolCall], TokenUsage]:
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}

        content_parts: list[str] = []
        # tool_calls are accumulated by index
        tc_map: dict[int, dict[str, Any]] = {}
        usage = TokenUsage()

        stream = await self._client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if chunk.usage is not None:
                usage = _extract_usage_from_obj(chunk.usage)

            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            # --- streamed text ---
            if delta.content:
                on_text_chunk(delta.content)
                content_parts.append(delta.content)

            # --- streamed tool calls ---
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tc_map:
                        tc_map[idx] = {
                            "id": tc_delta.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    entry = tc_map[idx]
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            entry["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            entry["arguments"] += tc_delta.function.arguments

        text = "".join(content_parts)
        tool_calls = _tc_map_to_list(tc_map)
        return text, tool_calls, usage


# ======================================================================
# Conversion helpers (module-level, easy to unit-test)
# ======================================================================


def _build_messages(
    system_prompt: str,
    messages: list["Message"],
    cache_prefix: str = "",
) -> list[dict[str, Any]]:
    """Convert nutshell Messages to OpenAI chat messages."""
    result: list[dict[str, Any]] = []

    # System prompt is the first message with role=system
    full_system = (
        (cache_prefix + "\n" + system_prompt).strip()
        if cache_prefix
        else system_prompt
    )
    if full_system:
        result.append({"role": "system", "content": full_system})

    for msg in messages:
        if msg.role == "tool":
            # Tool results: OpenAI expects role=tool with tool_call_id
            # nutshell encodes tool results as list content blocks
            if isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        result.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": block.get("content", ""),
                        })
                    else:
                        # Fallback: treat as plain text
                        result.append({"role": "user", "content": str(block)})
            else:
                result.append({"role": "tool", "tool_call_id": "", "content": str(msg.content)})
        elif msg.role == "assistant":
            # Assistant messages may contain tool_calls
            if isinstance(msg.content, list):
                text_parts = []
                tool_calls_api = []
                for block in msg.content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_use":
                            tool_calls_api.append({
                                "id": block["id"],
                                "type": "function",
                                "function": {
                                    "name": block["name"],
                                    "arguments": json.dumps(block.get("input", {})),
                                },
                            })
                        elif block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                    else:
                        text_parts.append(str(block))
                entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": "".join(text_parts) or None,
                }
                if tool_calls_api:
                    entry["tool_calls"] = tool_calls_api
                result.append(entry)
            else:
                result.append({"role": "assistant", "content": msg.content})
        else:
            # user messages
            if isinstance(msg.content, list):
                # Flatten to text for OpenAI
                parts = []
                for block in msg.content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block["text"])
                    elif isinstance(block, str):
                        parts.append(block)
                    else:
                        parts.append(str(block))
                result.append({"role": "user", "content": "".join(parts)})
            else:
                result.append({"role": "user", "content": msg.content})

    return result


def _tool_to_openai(tool: "Tool") -> dict[str, Any]:
    """Convert a nutshell Tool to OpenAI function-calling format."""
    api = tool.to_api_dict()
    return {
        "type": "function",
        "function": {
            "name": api["name"],
            "description": api.get("description", ""),
            "parameters": api.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


def _parse_response(
    response: Any,
) -> tuple[str, list[ToolCall], TokenUsage]:
    """Parse a non-streaming ChatCompletion response."""
    choice = response.choices[0] if response.choices else None
    text = ""
    tool_calls: list[ToolCall] = []

    if choice and choice.message:
        msg = choice.message
        text = msg.content or ""
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(
                    ToolCall(id=tc.id, name=tc.function.name,
                             input=_parse_json_args(tc.function.arguments or ""))
                )

    usage = TokenUsage()
    if response.usage:
        usage = _extract_usage_from_obj(response.usage)

    return text, tool_calls, usage


def _extract_usage_from_obj(usage: Any) -> TokenUsage:
    """Extract TokenUsage from an OpenAI usage object."""
    return TokenUsage(
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "prompt_tokens_details", None)
        and getattr(usage.prompt_tokens_details, "cached_tokens", 0)
        or 0,
        cache_write_tokens=0,
    )


def _tc_map_to_list(tc_map: dict[int, dict[str, Any]]) -> list[ToolCall]:
    """Convert accumulated streaming tool-call fragments to ToolCall list."""
    return [
        ToolCall(id=entry["id"], name=entry["name"],
                 input=_parse_json_args(entry["arguments"]))
        for idx in sorted(tc_map)
        for entry in (tc_map[idx],)
    ]
