"""CodexProvider — calls OpenAI Codex via ChatGPT OAuth.

Reads credentials from ~/.butterfly/auth.json (butterfly's own auth store,
written by ``butterfly codex login``).  On first use, tokens are automatically
migrated from ~/.codex/auth.json if that file exists, so users upgrading from
an older butterfly that read the Codex CLI's file don't need to log in again.

Storing tokens in butterfly's own file keeps refresh-token rotation independent
of the Codex CLI and VS Code extension — a refresh by those tools no longer
invalidates butterfly's session (and vice versa).

auto-refreshes the access token when it expires, and calls:
  POST https://chatgpt.com/backend-api/codex/responses

The response format is the OpenAI Responses API over SSE, not Chat Completions.

Behavioral notes (aligned with openai/codex rust CLI `codex-rs`):
  * Default model is ``gpt-5.4``. (codex-rs defaults to ``gpt-5-codex``, but
    the ChatGPT-OAuth backend rejects ``gpt-5-codex`` with a 400
    — confirmed by live test on 2026-04-15 — so we keep the legacy
    working default here until the backend advertises support.)
  * When ``thinking=True`` we send ``include=["reasoning.encrypted_content"]``
    and re-echo reasoning items on subsequent turns so the server can retain
    chain-of-thought across turns.
  * ``prompt_cache_key`` and the ``session_id`` header are sent on every
    request. NOTE: on the ChatGPT-OAuth backend, no cache_read_tokens have
    been observed in practice even with a stable 1000+ token prefix; we keep
    the fields because they are the officially documented signal and a
    future backend change may start honoring them, but callers should not
    rely on cache hits today.
  * ``response.failed`` / ``response.incomplete`` are mapped to the butterfly
    error taxonomy.
  * A single auto-refresh + retry is attempted on 401 mid-stream.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, ClassVar

from butterfly.core.provider import Provider
from butterfly.core.types import TokenUsage, ToolCall
from butterfly.llm_engine.errors import (
    AuthError,
    BadRequestError,
    ContextWindowExceededError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
    ServerError,
)
from butterfly.llm_engine.providers._common import _parse_json_args, stringify_tool_result_content

if TYPE_CHECKING:
    from butterfly.core.types import Message
    from butterfly.core.tool import Tool

# Butterfly's own Codex auth store — separate from ~/.codex/auth.json so
# refresh-token rotation in the Codex CLI or VS Code extension does NOT
# invalidate butterfly's session (and vice versa).
_AUTH_PATH = Path.home() / ".butterfly" / "auth.json"
# Fallback / migration source: tokens originally written by `codex login`.
_CODEX_CLI_AUTH_PATH = Path.home() / ".codex" / "auth.json"
_VALID_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
_CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
_TOKEN_URL = "https://auth.openai.com/oauth/token"
_DEVICE_USERCODE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
_DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_JWT_AUTH_CLAIM = "https://api.openai.com/auth"
_DEFAULT_READ_TIMEOUT = 600.0  # gpt-5 xhigh routinely exceeds 120s before first token
_ORIGINATOR = "codex_cli_rs"  # matches openai/codex; "pi" is not on the server allowlist
_MAX_SSE_BUFFER_BYTES = 1_048_576  # 1 MiB — defend against a server that never sends \n\n

# Module-level lock serializes token-refresh writes to ~/.butterfly/auth.json
# across concurrent providers in the same process. File-level cross-process
# locking is out of scope — the CLI is the only other writer.
_REFRESH_LOCK = asyncio.Lock()


# Explicit allow-list: only OpenAI-family model names are forwarded. Anything
# else (Anthropic / Kimi / Gemini / typos / legacy names) falls back to
# ``DEFAULT_MODEL``. Covers gpt-*, o-series (o1/o3/o4/...), codex-*, fine-tunes.
_CODEX_MODEL_ALLOW_RE = re.compile(r"^(gpt-|o\d+(-|$)|codex-|ft:gpt-)", re.IGNORECASE)


def _is_codex_compatible_model(model: str | None) -> bool:
    """True if *model* is an OpenAI/Codex-family name the backend accepts.

    Returns False for empty/None/whitespace and for any name that doesn't
    match the explicit allow-list (Anthropic, Kimi, Gemini, typos...). In
    those cases the caller falls back to ``DEFAULT_MODEL``.
    """
    if not model:
        return False
    stripped = model.strip()
    if not stripped:
        return False
    return bool(_CODEX_MODEL_ALLOW_RE.match(stripped))


class CodexProvider(Provider):
    """LLM provider backed by OpenAI Codex via ChatGPT Plus OAuth."""

    _supports_thinking: ClassVar[bool] = True
    # ChatGPT-OAuth backend rejects "gpt-5-codex" with 400 as of 2026-04-15 even
    # though codex-rs defaults to it — keep gpt-5.4 here until the backend
    # supports codex model IDs. See docstring for details.
    DEFAULT_MODEL: ClassVar[str] = "gpt-5.4"

    def __init__(self, max_tokens: int | None = None) -> None:
        # The ChatGPT-OAuth backend rejects ``max_output_tokens`` with HTTP
        # 400 even though the public Responses API accepts it. Make the field
        # opt-in: ``None`` (default) skips emission; pass an explicit integer
        # only when the target endpoint supports it.
        self.max_tokens = max_tokens
        # Per-instance conversation id — used for prompt_cache_key + session_id header.
        # One provider instance = one conversation bucket from the server's POV.
        self._conversation_id = str(uuid.uuid4())
        # Reasoning items captured from the last stream; drained by consume_extra_blocks().
        self._pending_reasoning: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Provider interface
    # ------------------------------------------------------------------

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
        on_thinking_start: Callable[[], None] | None = None,
        on_thinking_end: Callable[[str], None] | None = None,
        cache_system_prefix: str = "",
        cache_last_human_turn: bool = False,
        thinking: bool = False,
        thinking_budget: int = 8000,  # ignored — Codex uses effort, not budget_tokens
        thinking_effort: str = "high",
    ) -> tuple[str, list[ToolCall], TokenUsage]:
        full_system = (
            (cache_system_prefix + "\n\n" + system_prompt).strip()
            if cache_system_prefix
            else system_prompt
        )
        effective_model = model if _is_codex_compatible_model(model) else self.DEFAULT_MODEL
        effort = thinking_effort if thinking_effort in _VALID_EFFORTS else "medium"
        body = _build_request_body(
            effective_model,
            full_system,
            messages,
            tools,
            thinking=thinking,
            thinking_effort=effort,
            prompt_cache_key=self._conversation_id,
            max_output_tokens=self.max_tokens,
        )

        import httpx

        timeout = httpx.Timeout(connect=10.0, read=_DEFAULT_READ_TIMEOUT, write=30.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            last_access_token = ""
            for attempt in range(2):
                access_token, account_id = await self._get_auth_async(
                    force_refresh=attempt > 0, rejected_token=last_access_token
                )
                last_access_token = access_token
                headers = _build_headers(access_token, account_id, self._conversation_id)
                try:
                    async with client.stream("POST", _CODEX_URL, headers=headers, json=body) as resp:
                        status = resp.status_code
                        if status == 401 and attempt == 0:
                            await resp.aread()
                            continue
                        if status == 401:
                            # Second attempt after a forced refresh still 401 →
                            # refresh token itself is the problem. Emit the
                            # descriptive message instead of the generic
                            # "Codex API error 401".
                            await resp.aread()
                            raise AuthError(
                                "Codex authentication failed even after token refresh. "
                                "Run `codex login` to re-authenticate.",
                                provider="codex-oauth",
                                status=401,
                            )
                        if status != 200:
                            body_bytes = await resp.aread()
                            _raise_from_status(
                                status, body_bytes.decode("utf-8", errors="replace")[:500]
                            )
                        text, tool_calls, usage, reasoning_items = await _parse_sse_stream(
                            resp, on_text_chunk,
                            on_thinking_start=on_thinking_start,
                            on_thinking_end=on_thinking_end,
                        )
                        self._pending_reasoning = reasoning_items
                        return text, tool_calls, usage
                except httpx.TimeoutException as exc:
                    raise ProviderTimeoutError(
                        f"Codex request timed out: {exc}", provider="codex-oauth"
                    ) from exc

        # Every control-flow path inside the `for attempt in range(2)` loop
        # either returns (success) or raises (401-after-refresh, non-200
        # status, timeout). The loop never falls through.
        raise AssertionError("unreachable: codex retry loop fell through")  # pragma: no cover

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    async def _get_auth_async(
        self,
        *,
        force_refresh: bool = False,
        rejected_token: str = "",
    ) -> tuple[str, str]:
        """Return (access_token, account_id), refreshing under the module lock.

        Under the lock we re-read the auth file — if another coroutine just
        refreshed while we were queued, we reuse its token. ``rejected_token``
        carries the token the server just 401'd on; if the file token is
        still that same string, we force a refresh; otherwise the token
        changed under us and we try it first.
        """
        async with _REFRESH_LOCK:
            auth = _read_auth()
            tokens = auth.get("tokens", {})
            access_token = tokens.get("access_token", "")
            refresh_token = tokens.get("refresh_token", "")

            needs_refresh = _is_token_expired(access_token) or (
                force_refresh and access_token and access_token == rejected_token
            )
            if needs_refresh:
                if not refresh_token:
                    raise AuthError(
                        "Codex access token expired and no refresh_token available. "
                        "Run `codex login` to re-authenticate.",
                        provider="codex-oauth",
                        status=401,
                    )
                tokens = await _refresh_access_token_async(refresh_token)
                auth["tokens"] = tokens
                _write_auth(auth)
                access_token = tokens["access_token"]

        account_id = _extract_account_id(access_token, tokens.get("id_token", ""))
        return access_token, account_id


# ======================================================================
# Auth I/O
# ======================================================================


def _read_auth() -> dict[str, Any]:
    """Read Codex OAuth tokens from butterfly's own auth store.

    On first use, migrates tokens from the Codex CLI's ``~/.codex/auth.json``
    if butterfly's own ``~/.butterfly/auth.json`` doesn't exist yet.  This
    one-time migration avoids requiring a fresh login after upgrading.  After
    migration butterfly manages its own refresh-token rotation independently of
    the Codex CLI and VS Code extension.
    """
    if not _AUTH_PATH.exists():
        # One-time migration: import tokens from the Codex CLI's auth file.
        if _CODEX_CLI_AUTH_PATH.exists():
            try:
                data = json.loads(_CODEX_CLI_AUTH_PATH.read_text(encoding="utf-8"))
                tokens = data.get("tokens", {})
                if isinstance(tokens, dict) and tokens.get("access_token") and tokens.get("refresh_token"):
                    _write_auth(data)
                    return data
            except Exception:
                pass
        raise AuthError(
            f"Codex auth file not found at {_AUTH_PATH}. "
            "Run `butterfly codex login` to authenticate.",
            provider="codex-oauth",
            status=401,
        )
    return json.loads(_AUTH_PATH.read_text(encoding="utf-8"))


def _write_auth(auth: dict[str, Any]) -> None:
    _AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    _AUTH_PATH.write_text(json.dumps(auth, indent=2), encoding="utf-8")
    # Restrict to owner-only; OAuth refresh tokens must not be world-readable.
    try:
        os.chmod(_AUTH_PATH, 0o600)
    except OSError:
        pass


def _is_token_expired(token: str, buffer_seconds: int = 300) -> bool:
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


def _extract_account_id(access_token: str, id_token: str = "") -> str:
    """Extract chatgpt_account_id from access_token, with id_token as fallback."""
    for token in (access_token, id_token):
        if not token:
            continue
        try:
            parts = token.split(".")
            payload = json.loads(_b64decode_pad(parts[1]))
            account_id = payload.get(_JWT_AUTH_CLAIM, {}).get("chatgpt_account_id", "")
            if account_id:
                return account_id
        except Exception:
            continue
    raise AuthError(
        "Failed to extract chatgpt_account_id from Codex tokens",
        provider="codex-oauth",
        status=401,
    )


async def _refresh_access_token_async(refresh_token: str) -> dict[str, str]:
    """Async token refresh via httpx — does not block the event loop."""
    import httpx

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _CLIENT_ID,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _TOKEN_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise AuthError(
            f"Codex token refresh transport error: {exc}. "
            "Check network / proxy configuration.",
            provider="codex-oauth",
            status=None,
        ) from exc

    if resp.status_code != 200:
        body = resp.text[:200] if resp.text else ""
        raise AuthError(
            f"Codex token refresh failed ({resp.status_code}): {body or resp.reason_phrase}. "
            "The refresh token may be expired or revoked — run `codex login` again.",
            provider="codex-oauth",
            status=resp.status_code,
        )
    result = resp.json()

    if "access_token" not in result or "refresh_token" not in result:
        raise AuthError(
            f"Codex token refresh returned unexpected payload: {result}",
            provider="codex-oauth",
            status=401,
        )

    return {
        "access_token": result["access_token"],
        "refresh_token": result["refresh_token"],
        "id_token": result.get("id_token", ""),
        "account_id": _extract_account_id(result["access_token"], result.get("id_token", "")),
    }


# ======================================================================
# HTTP helpers
# ======================================================================


def _build_headers(access_token: str, account_id: str, conversation_id: str) -> dict[str, str]:
    try:
        sysname = os.uname().sysname.lower()
    except AttributeError:
        sysname = "unknown"
    return {
        "Authorization": f"Bearer {access_token}",
        "ChatGPT-Account-ID": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": _ORIGINATOR,
        "session_id": conversation_id,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": f"butterfly/2.0 ({sysname})",
    }


def _build_request_body(
    model: str,
    system_prompt: str,
    messages: list["Message"],
    tools: list["Tool"],
    thinking: bool = False,
    thinking_effort: str = "high",
    prompt_cache_key: str = "",
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
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
    if max_output_tokens and max_output_tokens > 0:
        body["max_output_tokens"] = max_output_tokens
    if prompt_cache_key:
        body["prompt_cache_key"] = prompt_cache_key
    if thinking:
        body["reasoning"] = {"effort": thinking_effort, "summary": "auto"}
        body["include"] = ["reasoning.encrypted_content"]
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
        "strict": False,
    }


def _raise_from_status(status: int, body: str) -> None:
    msg = f"Codex API error {status}" + (f": {body}" if body else "")
    if status in (401, 403):
        raise AuthError(msg, provider="codex-oauth", status=status)
    if status == 400:
        raise BadRequestError(msg, provider="codex-oauth", status=status)
    if status == 429:
        raise RateLimitError(
            msg,
            provider="codex-oauth",
            status=status,
            retry_after=_parse_retry_after(body),
        )
    if 500 <= status < 600:
        raise ServerError(msg, provider="codex-oauth", status=status)
    raise ProviderError(msg, provider="codex-oauth", status=status)


# ======================================================================
# Message conversion (butterfly → Responses API)
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
        return [_assistant_message_item(content)]
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
            # Replay reasoning verbatim: server validates id + encrypted_content
            # against the previous turn when `include=reasoning.encrypted_content`
            # was set on that request.
            flush_text()
            item: dict[str, Any] = {
                "type": "reasoning",
                "id": block.get("id") or f"rs_{uuid.uuid4().hex[:24]}",
                # ``dict.get`` returns the default only when the key is
                # ABSENT; ``None`` passes through. Coerce with ``or []`` so
                # the server never sees ``summary: null`` (schema expects an
                # array).
                "summary": block.get("summary") or [],
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
            text = stringify_tool_result_content(block.get("content", ""))
            result.append({
                "type": "function_call_output",
                "call_id": tool_use_id,
                "output": text,
            })
    return result


# ======================================================================
# SSE streaming parser
# ======================================================================


def _extract_summary_text(item: dict[str, Any]) -> str:
    """Pull joined summary text out of a Codex reasoning item.

    Reasoning items end with a ``summary`` array of ``{type, text}`` parts.
    Returns the joined text body, or empty string if no summary is present.
    """
    parts = item.get("summary") or []
    if not isinstance(parts, list):
        return ""
    pieces: list[str] = []
    for p in parts:
        if isinstance(p, dict):
            text = p.get("text", "")
            if text:
                pieces.append(text)
    return "\n\n".join(pieces)


async def _parse_sse_stream(
    resp: Any,
    on_text_chunk: Callable[[str], None] | None,
    *,
    on_thinking_start: Callable[[], None] | None = None,
    on_thinking_end: Callable[[str], None] | None = None,
) -> tuple[str, list[ToolCall], TokenUsage, list[dict[str, Any]]]:
    text_parts: list[str] = []
    tc_map: dict[str, dict[str, str]] = {}
    current_tc_id: str | None = None
    # Track the open output_item's type so output_text.delta events fired
    # inside a "reasoning" item can be routed to thinking instead of leaking
    # into the assistant text channel. The backend has been observed to
    # stream reasoning summary via output_text.delta inside a wrapping
    # reasoning item rather than via reasoning_summary_text.delta.
    current_output_item_type: str | None = None
    reasoning_items: list[dict[str, Any]] = []
    usage = TokenUsage()
    # Thinking block lifecycle: buffer reasoning deltas locally; deliver as
    # a single block via on_thinking_end when the item closes. Never leaks
    # into on_text_chunk (which must remain assistant-text only).
    thinking_parts: list[str] = []
    thinking_active = False
    debug_etypes = os.environ.get("BUTTERFLY_CODEX_DEBUG") == "1"

    def _start_thinking() -> None:
        nonlocal thinking_active
        if thinking_active:
            return
        thinking_active = True
        thinking_parts.clear()
        if on_thinking_start is not None:
            try:
                on_thinking_start()
            except Exception:  # noqa: BLE001
                pass

    def _end_thinking() -> None:
        nonlocal thinking_active
        if not thinking_active:
            return
        body = "".join(thinking_parts)
        thinking_parts.clear()
        thinking_active = False
        if on_thinking_end is not None:
            try:
                on_thinking_end(body)
            except Exception:  # noqa: BLE001
                pass

    def _process_block(block: str) -> None:
        """Parse one SSE block (text up to a \\n\\n boundary) and apply events."""
        nonlocal current_tc_id, current_output_item_type, usage
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
                itype = item.get("type")
                current_output_item_type = itype
                if itype == "function_call":
                    call_id = item.get("call_id", str(uuid.uuid4()))
                    tc_map[call_id] = {"name": item.get("name", ""), "args": ""}
                    current_tc_id = call_id
                elif itype == "reasoning":
                    _start_thinking()

            elif etype == "response.function_call_arguments.delta":
                delta_call_id = event.get("call_id") or current_tc_id
                if delta_call_id and delta_call_id in tc_map:
                    tc_map[delta_call_id]["args"] += event.get("delta", "")

            elif etype == "response.output_item.done":
                item = event.get("item", {})
                itype = item.get("type", "")
                if itype == "function_call":
                    call_id = item.get("call_id", "")
                    if call_id in tc_map:
                        tc_map[call_id]["args"] = item.get("arguments", tc_map[call_id]["args"])
                    current_tc_id = None
                elif itype == "reasoning":
                    captured: dict[str, Any] = {
                        "type": "reasoning",
                        "id": item.get("id") or "",
                        "summary": item.get("summary") or [],
                    }
                    if "encrypted_content" in item:
                        captured["encrypted_content"] = item["encrypted_content"]
                    reasoning_items.append(captured)
                    # Some Codex turns emit the reasoning item.done without a
                    # prior item.added (pure encrypted reasoning). Treat the
                    # .done event itself as the open+close lifecycle so the UI
                    # still gets a "Thought for Xs" pill.
                    if not thinking_active:
                        _start_thinking()
                    # Fallback: if no streaming reasoning deltas arrived (or
                    # they came on an unrecognized etype), pull the body
                    # directly from item.summary[].text so the UI gets the
                    # full thinking content rather than an empty cell.
                    if not thinking_parts:
                        extracted = _extract_summary_text(item)
                        if extracted:
                            thinking_parts.append(extracted)
                    _end_thinking()
                current_output_item_type = None

            elif etype == "response.output_text.delta":
                delta = event.get("delta", "")
                if delta:
                    # When the open output_item is a reasoning wrapper, route
                    # this delta to thinking — backend has been observed to
                    # stream reasoning summary content through this channel.
                    if current_output_item_type == "reasoning":
                        if not thinking_active:
                            _start_thinking()
                        thinking_parts.append(delta)
                    else:
                        text_parts.append(delta)
                        if on_text_chunk:
                            on_text_chunk(delta)

            elif etype == "response.reasoning_text.delta":
                delta = event.get("delta", "")
                if delta:
                    if not thinking_active:
                        _start_thinking()
                    thinking_parts.append(delta)

            elif etype == "response.reasoning_summary_text.delta":
                delta = event.get("delta", "")
                if delta:
                    if not thinking_active:
                        _start_thinking()
                    thinking_parts.append(delta)

            elif etype in ("response.completed", "response.done"):
                resp_data = event.get("response", {})
                usage = _extract_usage(resp_data.get("usage") or {})

            elif etype == "response.incomplete":
                resp_data = event.get("response", {})
                reason = (
                    (resp_data.get("incomplete_details") or {}).get("reason")
                    or event.get("incomplete_details", {}).get("reason")
                    or "incomplete"
                )
                if reason == "context_length":
                    raise ContextWindowExceededError(
                        "Codex response incomplete: context length exceeded",
                        provider="codex-oauth",
                    )
                raise ProviderError(
                    f"Codex response incomplete: {reason}", provider="codex-oauth"
                )

            elif etype in ("error", "response.failed"):
                _raise_stream_error(event)

            elif etype.startswith("response.reasoning") or "summary" in etype:
                # Catch-all for reasoning/summary event variants we don't
                # explicitly handle (backend has shipped new event names
                # historically). Pull a delta or text body from common fields
                # and route to thinking; never leak to assistant text.
                delta = event.get("delta", "") or event.get("text", "")
                if not delta:
                    part = event.get("part") or event.get("item") or {}
                    if isinstance(part, dict):
                        delta = part.get("text", "") or _extract_summary_text(part)
                if delta:
                    if not thinking_active:
                        _start_thinking()
                    thinking_parts.append(delta)
                if debug_etypes:
                    print(f"[codex catch-all reasoning] {etype}", flush=True)

            elif debug_etypes:
                # Surface any unhandled event type when debugging is on, so a
                # backend change can be diagnosed without invasive logging.
                print(f"[codex unhandled etype] {etype}", flush=True)

    buffer = ""
    async for raw_chunk in resp.aiter_bytes():
        buffer += raw_chunk.decode("utf-8", errors="replace")
        # Bound the buffer so a server that never sends \n\n can't exhaust
        # memory. 1 MiB is ~10× the largest legitimate SSE event.
        if len(buffer) > _MAX_SSE_BUFFER_BYTES:
            raise ProviderError(
                f"Codex SSE buffer exceeded {_MAX_SSE_BUFFER_BYTES} bytes "
                "without an event boundary — aborting.",
                provider="codex-oauth",
            )
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            _process_block(block)

    # The connection may close mid-event without sending the final \n\n.
    # Try to parse the remaining buffer as one last event so a tail event
    # (e.g. the final response.completed) isn't silently dropped. Malformed
    # JSON is skipped by _process_block.
    if buffer.strip():
        _process_block(buffer)

    # If the stream closed with a reasoning block still open (shouldn't
    # normally happen — output_item.done is always emitted — but guard
    # anyway so the UI never gets a dangling "Thinking…" spinner).
    if thinking_active:
        _end_thinking()

    text = "".join(text_parts)
    tool_calls = [
        ToolCall(id=call_id, name=tc["name"], input=_parse_json_args(tc["args"]))
        for call_id, tc in tc_map.items()
        if tc["name"]
    ]
    return text, tool_calls, usage, reasoning_items


def _extract_usage(u: dict[str, Any]) -> TokenUsage:
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
    reasoning = (u.get("output_tokens_details") or {}).get("reasoning_tokens", 0) or 0
    input_raw = u.get("input_tokens") or 0
    return TokenUsage(
        input_tokens=max(input_raw - cached, 0),
        output_tokens=u.get("output_tokens") or 0,
        cache_read_tokens=cached,
        cache_write_tokens=0,
        reasoning_tokens=reasoning,
    )


_CTX_LENGTH_CODES = frozenset({
    "context_length_exceeded",
    "context_window_exceeded",
    "content_too_long",
    "max_tokens_exceeded",
})
_RATE_LIMIT_CODES = frozenset({
    "rate_limit_exceeded",
    "quota_exceeded",
    "insufficient_quota",
    "requests_rate_limit",
    "tokens_rate_limit",
})
_AUTH_CODES = frozenset({
    "invalid_api_key",
    "invalid_token",
    "expired_token",
    "unauthorized",
    "authentication_error",
    "authentication_failed",
})
_SERVER_CODES = frozenset({
    "server_overloaded",
    "server_error",
    "internal_server_error",
    "service_unavailable",
})


def _raise_stream_error(event: dict[str, Any]) -> None:
    """Classify a response.failed / error event into the butterfly error taxonomy.

    Match strategy: prefer exact ``code`` matching on a small enumerated set
    of known OpenAI/Codex error codes; fall back to phrase matching only on
    the message. Avoids substring false-positives like ``authority_not_found``
    → AuthError or ``invalid_server_url`` → ServerError.
    """
    err_obj: dict[str, Any] = {}
    resp_data = event.get("response") or {}
    if isinstance(resp_data, dict):
        err_obj = resp_data.get("error") or {}
    message = (
        event.get("message")
        or err_obj.get("message")
        or json.dumps(event)[:300]
    )
    code = (event.get("code") or err_obj.get("code") or "").lower()
    msg_l = (message or "").lower()

    if code in _CTX_LENGTH_CODES or "context length exceeded" in msg_l or "context window" in msg_l:
        raise ContextWindowExceededError(
            f"Codex context length exceeded: {message}", provider="codex-oauth"
        )
    if code in _RATE_LIMIT_CODES or "rate limit" in msg_l or "quota exceeded" in msg_l:
        raise RateLimitError(
            f"Codex rate/quota error: {message}",
            provider="codex-oauth",
            retry_after=_parse_retry_after(message),
        )
    if code in _AUTH_CODES:
        raise AuthError(f"Codex auth error: {message}", provider="codex-oauth", status=401)
    if code in _SERVER_CODES or "server overloaded" in msg_l:
        raise ServerError(f"Codex server error: {message}", provider="codex-oauth")
    raise ProviderError(f"Codex stream error: {message}", provider="codex-oauth")


_RETRY_AFTER_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(milliseconds?|ms|seconds?|secs?|s|minutes?|mins?|m)\b"
)


def _parse_retry_after(text: str) -> float | None:
    """Best-effort extraction of a retry-after value from a free-form message."""
    if not text:
        return None
    m = _RETRY_AFTER_RE.search(text.lower())
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2)
    if unit.startswith("m") and (unit.startswith("mil") or unit == "ms"):
        return value / 1000.0
    if unit.startswith("m"):  # minutes
        return value * 60.0
    return value  # seconds
