"""CodexProvider — calls OpenAI Codex via ChatGPT OAuth.

Reads credentials from ~/.codex/auth.json (written by the ``codex`` CLI),
auto-refreshes the access token when it expires, and calls:
  POST https://chatgpt.com/backend-api/codex/responses

The response format is the OpenAI Responses API over SSE, not Chat Completions.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, ClassVar

from nutshell.core.provider import Provider
from nutshell.core.types import TokenUsage, ToolCall
from nutshell.llm_engine.providers._common import _parse_json_args

if TYPE_CHECKING:
    from nutshell.core.types import Message
    from nutshell.core.tool import Tool

_AUTH_PATH = Path.home() / ".codex" / "auth.json"
_VALID_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
_CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_JWT_AUTH_CLAIM = "https://api.openai.com/auth"


class CodexProvider(Provider):
    """LLM provider backed by OpenAI Codex via ChatGPT Plus OAuth.

    Uses ``~/.codex/auth.json`` written by the official ``codex`` CLI.
    Access tokens are refreshed automatically.

    Default model: gpt-5.4 with high reasoning effort when thinking=True.
    """

    _supports_thinking: ClassVar[bool] = True
    # Default model for the Codex OAuth endpoint.
    # Passed through to complete() when the session model is empty or the
    # generic fallback ("claude-sonnet-4-6").
    DEFAULT_MODEL: ClassVar[str] = "gpt-5.4"

    def __init__(self, max_tokens: int = 8096) -> None:
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
        thinking_budget: int = 8000,  # ignored — Codex uses effort, not budget_tokens
        thinking_effort: str = "high",
    ) -> tuple[str, list[ToolCall], TokenUsage]:
        access_token, account_id = self._get_auth()
        headers = _build_headers(access_token, account_id)
        full_system = (
            (cache_system_prefix + "\n\n" + system_prompt).strip()
            if cache_system_prefix
            else system_prompt
        )
        # Fall back to DEFAULT_MODEL when no model is configured for this provider
        effective_model = model if model and model != "claude-sonnet-4-6" else self.DEFAULT_MODEL
        # Validate effort level — silently clamp to "high" if caller passes an invalid value
        effort = thinking_effort if thinking_effort in _VALID_EFFORTS else "high"
        body = _build_request_body(effective_model, full_system, messages, tools, thinking=thinking, thinking_effort=effort)

        import httpx

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", _CODEX_URL, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    error_text = await resp.aread()
                    raise RuntimeError(
                        f"Codex API error {resp.status_code}: {error_text.decode()[:500]}"
                    )
                return await _parse_sse_stream(resp, on_text_chunk)

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _get_auth(self) -> tuple[str, str]:
        """Return (access_token, account_id), refreshing if needed."""
        auth = _read_auth()
        tokens = auth.get("tokens", {})
        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")

        if _is_token_expired(access_token):
            if not refresh_token:
                raise RuntimeError(
                    "Codex access token expired and no refresh_token available. "
                    "Run `codex login` to re-authenticate."
                )
            tokens = _refresh_access_token(refresh_token)
            auth["tokens"] = tokens
            _write_auth(auth)
            access_token = tokens["access_token"]

        account_id = _extract_account_id(access_token)
        return access_token, account_id


# ======================================================================
# Auth I/O
# ======================================================================


def _read_auth() -> dict[str, Any]:
    if not _AUTH_PATH.exists():
        raise RuntimeError(
            f"Codex auth file not found at {_AUTH_PATH}. "
            "Run `codex login` to authenticate."
        )
    return json.loads(_AUTH_PATH.read_text(encoding="utf-8"))


def _write_auth(auth: dict[str, Any]) -> None:
    _AUTH_PATH.write_text(json.dumps(auth, indent=2), encoding="utf-8")


def _is_token_expired(token: str, buffer_seconds: int = 300) -> bool:
    """Return True if JWT is expired or will expire within buffer_seconds."""
    if not token:
        return True
    try:
        parts = token.split(".")
        payload = json.loads(_b64decode_pad(parts[1]))
        exp = payload.get("exp", 0)
        return time.time() + buffer_seconds >= exp
    except Exception:
        return True


def _b64decode_pad(s: str) -> bytes:
    import base64
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


def _extract_account_id(token: str) -> str:
    try:
        parts = token.split(".")
        payload = json.loads(_b64decode_pad(parts[1]))
        account_id = payload.get(_JWT_AUTH_CLAIM, {}).get("chatgpt_account_id", "")
        if not account_id:
            raise ValueError("No chatgpt_account_id in token")
        return account_id
    except Exception as exc:
        raise RuntimeError(f"Failed to extract account_id from Codex token: {exc}") from exc


def _refresh_access_token(refresh_token: str) -> dict[str, str]:
    import urllib.request

    # Official Codex uses JSON body (not form-urlencoded) per codex-rs/login/src/auth/manager.rs
    payload = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        _TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())

    if "access_token" not in result or "refresh_token" not in result:
        raise RuntimeError(f"Token refresh failed: {result}")

    return {
        "access_token": result["access_token"],
        "refresh_token": result["refresh_token"],
        "id_token": result.get("id_token", ""),
        "account_id": _extract_account_id(result["access_token"]),
    }


# ======================================================================
# HTTP helpers
# ======================================================================


def _build_headers(access_token: str, account_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": "pi",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "nutshell/1.0 (darwin)",
    }


def _build_request_body(
    model: str,
    system_prompt: str,
    messages: list["Message"],
    tools: list["Tool"],
    thinking: bool = False,
    thinking_effort: str = "high",
) -> dict[str, Any]:
    # Strip "openai-codex/" prefix if present — bare model id for the endpoint
    model_id = model.split("/")[-1] if "/" in model else model

    body: dict[str, Any] = {
        "model": model_id,
        "store": False,
        "stream": True,
        "instructions": system_prompt,
        "input": _convert_messages(messages),
        "text": {"verbosity": "medium"},
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    if thinking:
        # Codex Responses API uses reasoning.effort (not budget_tokens).
        # Values: "none" | "minimal" | "low" | "medium" | "high" | "xhigh"
        # Source: codex-rs/protocol/src/openai_models.rs ReasoningEffort enum
        body["reasoning"] = {"effort": thinking_effort}
    if tools:
        body["tools"] = [_tool_to_responses_api(t) for t in tools]
    return body


def _tool_to_responses_api(tool: "Tool") -> dict[str, Any]:
    api = tool.to_api_dict()
    return {
        "type": "function",
        "name": api["name"],
        "description": api.get("description", ""),
        "parameters": api.get("input_schema", {"type": "object", "properties": {}}),
        "strict": None,
    }


# ======================================================================
# Message conversion (nutshell → Responses API)
# ======================================================================


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
        return [{
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": content, "annotations": []}],
            "status": "completed",
        }]
    if not isinstance(content, list):
        return []

    result: list[dict[str, Any]] = []
    text_parts: list[str] = []

    for block in content:
        if not isinstance(block, dict):
            text_parts.append(str(block))
            continue
        btype = block.get("type", "")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            # Flush accumulated text first
            if text_parts:
                result.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "".join(text_parts), "annotations": []}],
                    "status": "completed",
                })
                text_parts = []
            tool_id = block.get("id", str(uuid.uuid4()))
            result.append({
                "type": "function_call",
                "id": f"fc_{tool_id}",
                "call_id": tool_id,
                "name": block.get("name", ""),
                "arguments": json.dumps(block.get("input", {})),
            })

    if text_parts:
        result.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "".join(text_parts), "annotations": []}],
            "status": "completed",
        })
    return result


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


# ======================================================================
# SSE streaming parser
# ======================================================================


async def _parse_sse_stream(
    resp: Any,
    on_text_chunk: Callable[[str], None] | None,
) -> tuple[str, list[ToolCall], TokenUsage]:
    text_parts: list[str] = []
    # tool call accumulation: call_id → {name, args_str}
    tc_map: dict[str, dict[str, str]] = {}
    current_tc_id: str | None = None
    usage = TokenUsage()

    buffer = ""
    async for raw_chunk in resp.aiter_bytes():
        buffer += raw_chunk.decode("utf-8", errors="replace")
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            data_lines = [
                line[5:].strip()
                for line in block.splitlines()
                if line.startswith("data:")
            ]
            for data in data_lines:
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")

                if etype == "response.output_item.added":
                    item = event.get("item", {})
                    if item.get("type") == "function_call":
                        call_id = item.get("call_id", str(uuid.uuid4()))
                        tc_map[call_id] = {"name": item.get("name", ""), "args": ""}
                        current_tc_id = call_id

                elif etype == "response.function_call_arguments.delta":
                    delta_call_id = event.get("call_id") or current_tc_id
                    if delta_call_id and delta_call_id in tc_map:
                        tc_map[delta_call_id]["args"] += event.get("delta", "")

                elif etype == "response.output_item.done":
                    item = event.get("item", {})
                    if item.get("type") == "function_call":
                        call_id = item.get("call_id", "")
                        if call_id in tc_map:
                            tc_map[call_id]["args"] = item.get("arguments", tc_map[call_id]["args"])
                        current_tc_id = None

                elif etype == "response.output_text.delta":
                    delta = event.get("delta", "")
                    if delta:
                        text_parts.append(delta)
                        if on_text_chunk:
                            on_text_chunk(delta)

                elif etype == "response.reasoning_text.delta":
                    # Streaming thinking/reasoning content — forward to on_text_chunk
                    # (same treatment as Anthropic's thinking_delta)
                    delta = event.get("delta", "")
                    if delta and on_text_chunk:
                        on_text_chunk(delta)

                elif etype in ("response.completed", "response.done"):
                    resp_data = event.get("response", {})
                    u = resp_data.get("usage", {})
                    cached = (u.get("input_tokens_details") or {}).get("cached_tokens", 0)
                    usage = TokenUsage(
                        input_tokens=(u.get("input_tokens") or 0) - cached,
                        output_tokens=u.get("output_tokens") or 0,
                        cache_read_tokens=cached,
                        cache_write_tokens=0,
                    )

                elif etype in ("error", "response.failed"):
                    msg = event.get("message") or event.get("response", {}).get("error", {}).get("message", "")
                    raise RuntimeError(f"Codex stream error: {msg or json.dumps(event)}")

    text = "".join(text_parts)
    tool_calls = [
        ToolCall(id=call_id, name=tc["name"], input=_parse_json_args(tc["args"]))
        for call_id, tc in tc_map.items()
        if tc["name"]
    ]
    return text, tool_calls, usage
