"""OpenAI Chat Completions provider.

For the Responses API (recommended for o-series / gpt-5 reasoning models),
see ``openai_responses.py``.
"""
from __future__ import annotations
import json
import os
import re
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


# Reasoning-family models want ``max_completion_tokens`` and ``reasoning_effort``,
# and reject ``temperature`` / ``top_p`` / ``presence_penalty`` / ``frequency_penalty``.
#
# Anchoring: ``gpt-5`` is followed only by end-of-string / ``.`` / ``-`` so
# ``gpt-5x-legacy`` (a hypothetical non-reasoning derivative) does NOT match.
# In contrast ``gpt-oss-*`` and ``o\d+-*`` match any suffix on purpose — every
# member of those families (``gpt-oss-20b``, ``gpt-oss-120b``, custom fine-tunes
# like ``gpt-oss-custom``, ``o3-mini``, ``o4-mini-high`` …) is a reasoning model.
_REASONING_MODEL_RE = re.compile(r"^(o\d+(?:$|-)|gpt-5(?:$|\.|-)|gpt-oss(?:$|-))", re.IGNORECASE)

# Valid reasoning_effort values per OpenAI schema.
_OPENAI_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "none"}

# Params the Chat Completions API rejects on reasoning models.
_REASONING_DISALLOWED_PARAMS = ("temperature", "top_p", "presence_penalty", "frequency_penalty", "logprobs", "top_logprobs")


def _is_reasoning_model(model: str) -> bool:
    return bool(_REASONING_MODEL_RE.match((model or "").strip()))


class OpenAIProvider(Provider):
    """LLM provider backed by OpenAI's Chat Completions API.

    Supports the official ``openai`` Python SDK. Works with standard API keys
    and with OAuth tokens (e.g. from ``openai-codex``).

    Environment variables
    ---------------------
    OPENAI_API_KEY   – API key or OAuth token (fallback when *api_key* is None)
    OPENAI_BASE_URL  – Optional custom endpoint
    """

    _supports_thinking: ClassVar[bool] = True

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 8096,
        max_retries: int = 3,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise ImportError(
                "Install the openai package: pip install 'openai>=1.0.0'"
            ) from None

        resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
        resolved_base = base_url or os.environ.get("OPENAI_BASE_URL") or None

        client_kwargs: dict[str, Any] = {
            "api_key": resolved_key,
            "max_retries": max_retries,
        }
        if resolved_base is not None:
            client_kwargs["base_url"] = resolved_base
        if default_headers is not None:
            client_kwargs["default_headers"] = default_headers

        self._client = AsyncOpenAI(**client_kwargs)
        self.max_tokens = max_tokens
        # Moonshot / Kimi-style ``reasoning_content`` captured from the last
        # completion. Drained by ``consume_extra_blocks`` so Agent.run can
        # attach it to the assistant message and echo it back on the next
        # turn — Kimi's API rejects assistant tool-call messages whose
        # ``reasoning_content`` is missing when thinking is enabled.
        # Always empty for standard OpenAI (which does not emit this field).
        self._pending_reasoning_content: str = ""

    def consume_extra_blocks(self) -> list[dict]:
        """Surface captured Moonshot/Kimi ``reasoning_content`` as a content block.

        The agent loop appends any returned blocks to the assistant message so
        they round-trip back to the provider on the next request — Kimi's
        API validates that every assistant message carrying ``tool_calls``
        includes the matching ``reasoning_content`` when thinking is on, so
        losing it between turns causes a 400 on every iteration after the
        first tool call. Standard OpenAI streams never populate this field
        so the default behaviour is unchanged there.
        """
        if not self._pending_reasoning_content:
            return []
        block = {
            "type": "reasoning_content",
            "text": self._pending_reasoning_content,
        }
        self._pending_reasoning_content = ""
        return [block]

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result

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
        thinking_budget: int = 8000,  # ignored — Chat Completions uses reasoning_effort
        thinking_effort: str = "high",
    ) -> tuple[str, list[ToolCall], TokenUsage]:
        # Standard Chat Completions has no thinking-visibility channel, but
        # Moonshot's OpenAI-compatible surface (used by KimiOpenAIProvider)
        # streams ``delta.reasoning_content`` for each reasoning token. The
        # stream parser picks those up and fires the hooks — plain OpenAI
        # streams never populate the field so the hooks stay silent there.
        api_messages = _build_messages(system_prompt, messages, cache_system_prefix)
        api_tools = [_tool_to_openai(t) for t in tools] if tools else []

        kwargs: dict[str, Any] = {"model": model, "messages": api_messages}
        _apply_model_specific_params(
            kwargs,
            model=model,
            max_tokens=self.max_tokens,
            thinking=thinking,
            thinking_effort=thinking_effort,
        )
        if api_tools:
            kwargs["tools"] = api_tools

        extra_body = self._extra_body_for_thinking(
            thinking=thinking,
            thinking_effort=thinking_effort,
            thinking_budget=thinking_budget,
        )
        if extra_body:
            merged = dict(kwargs.get("extra_body") or {})
            merged.update(extra_body)
            kwargs["extra_body"] = merged

        try:
            if on_text_chunk is not None:
                return await self._stream_complete(
                    kwargs,
                    on_text_chunk,
                    on_thinking_start=on_thinking_start,
                    on_thinking_end=on_thinking_end,
                )
            return await self._non_stream_complete(
                kwargs,
                on_thinking_start=on_thinking_start,
                on_thinking_end=on_thinking_end,
            )
        except Exception as exc:  # noqa: BLE001 - mapped below
            _maybe_raise_mapped_openai_error(exc)
            raise

    # ------------------------------------------------------------------
    # Subclass extension hooks
    # ------------------------------------------------------------------

    def _extra_body_for_thinking(
        self,
        *,
        thinking: bool,
        thinking_effort: str,
        thinking_budget: int,
    ) -> dict[str, Any] | None:
        """Return extra_body to merge into the request, or None.

        Default: no extra body. Subclasses whose backend enables thinking
        via ``extra_body`` (e.g. Kimi) override this to inject the vendor's
        thinking payload.
        """
        return None

    @staticmethod
    def _extract_usage(usage: Any) -> TokenUsage:
        """Extract TokenUsage from an OpenAI usage object.

        Exposed as a staticmethod so subclasses can customize usage
        extraction (e.g. Kimi, whose Moonshot API surfaces cached tokens at
        the top level in addition to ``prompt_tokens_details.cached_tokens``).
        """
        return _extract_usage_from_obj(usage)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _non_stream_complete(
        self,
        kwargs: dict[str, Any],
        *,
        on_thinking_start: Callable[[], None] | None = None,
        on_thinking_end: Callable[[str], None] | None = None,
    ) -> tuple[str, list[ToolCall], TokenUsage]:
        response = await self._client.chat.completions.create(**kwargs)
        # Moonshot returns the full reasoning body as ``message.reasoning_content``
        # on non-streaming responses. Standard OpenAI never populates it, so the
        # hook firing below collapses to a no-op for plain OpenAI.
        choice = response.choices[0] if response.choices else None
        reasoning_text = ""
        if choice is not None and choice.message is not None:
            reasoning_text = getattr(choice.message, "reasoning_content", "") or ""
        if reasoning_text:
            if on_thinking_start is not None:
                try:
                    on_thinking_start()
                except Exception:  # noqa: BLE001 - hooks are best-effort
                    pass
            if on_thinking_end is not None:
                try:
                    on_thinking_end(reasoning_text)
                except Exception:  # noqa: BLE001
                    pass
            self._pending_reasoning_content = reasoning_text
        else:
            self._pending_reasoning_content = ""
        return _parse_response(response, extract_usage=self._extract_usage)

    async def _stream_complete(
        self,
        kwargs: dict[str, Any],
        on_text_chunk: Callable[[str], None],
        *,
        on_thinking_start: Callable[[], None] | None = None,
        on_thinking_end: Callable[[str], None] | None = None,
    ) -> tuple[str, list[ToolCall], TokenUsage]:
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        thinking_opened = False  # guards on_thinking_start; prevents double-fire
        thinking_closed = False  # set when on_thinking_end has fired
        tc_map: dict[int, dict[str, Any]] = {}
        usage = TokenUsage()

        def _close_thinking() -> None:
            nonlocal thinking_closed
            if thinking_opened and not thinking_closed and on_thinking_end is not None:
                try:
                    on_thinking_end("".join(reasoning_parts))
                except Exception:  # noqa: BLE001 - best-effort hook
                    pass
                thinking_closed = True

        stream = await self._client.chat.completions.create(**kwargs)
        async for chunk in stream:
            chunk_usage = _chunk_usage(chunk)
            if chunk_usage is not None:
                usage = self._extract_usage(chunk_usage)

            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            # Moonshot / Kimi emits reasoning tokens as ``delta.reasoning_content``.
            # Accumulate them into ``reasoning_parts`` so the final body can be
            # exposed to the session IPC (thinking cell) and echoed back to the
            # provider on the next turn via ``consume_extra_blocks``.
            reasoning_delta = getattr(delta, "reasoning_content", None) or ""
            if reasoning_delta:
                if not thinking_opened:
                    thinking_opened = True
                    if on_thinking_start is not None:
                        try:
                            on_thinking_start()
                        except Exception:  # noqa: BLE001
                            pass
                reasoning_parts.append(reasoning_delta)

            if delta.content:
                # Assistant text begins — close the thinking cell so the UI
                # transitions from the "thinking…" pill to the streamed reply.
                _close_thinking()
                on_text_chunk(delta.content)
                content_parts.append(delta.content)

            if delta.tool_calls:
                # A tool call ends the thinking phase just as clearly as plain
                # text does — close the cell so the body isn't left hanging if
                # the assistant goes straight from reasoning into a tool call.
                _close_thinking()
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

        # Stream ended without a content/tool-call transition closing the
        # thinking cell — e.g. a reasoning-only finish (rare but legal).
        _close_thinking()

        self._pending_reasoning_content = "".join(reasoning_parts)
        text = "".join(content_parts)
        tool_calls = _tc_map_to_list(tc_map)
        return text, tool_calls, usage


# ======================================================================
# Conversion helpers (module-level, easy to unit-test)
# ======================================================================


def _apply_model_specific_params(
    kwargs: dict[str, Any],
    *,
    model: str,
    max_tokens: int,
    thinking: bool,
    thinking_effort: str,
) -> None:
    """Normalize per-model Chat Completions params.

    Reasoning-family models (o-series, gpt-5) want ``max_completion_tokens`` +
    ``reasoning_effort`` and reject ``temperature`` / ``top_p`` /
    ``presence_penalty`` / ``frequency_penalty`` / ``logprobs``. We scrub
    those unconditionally so upstream defaults or accidentally-passed params
    don't 400 the request. Legacy models keep ``max_tokens``.
    """
    if _is_reasoning_model(model):
        kwargs["max_completion_tokens"] = max_tokens
        for disallowed in _REASONING_DISALLOWED_PARAMS:
            kwargs.pop(disallowed, None)
        if thinking:
            effort = thinking_effort if thinking_effort in _OPENAI_EFFORTS else "medium"
            if effort != "none":
                kwargs["reasoning_effort"] = effort
    else:
        kwargs["max_tokens"] = max_tokens


def _build_messages(
    system_prompt: str,
    messages: list["Message"],
    cache_prefix: str = "",
) -> list[dict[str, Any]]:
    """Convert butterfly Messages to OpenAI chat messages."""
    result: list[dict[str, Any]] = []

    full_system = (
        (cache_prefix + "\n" + system_prompt).strip()
        if cache_prefix
        else system_prompt
    )
    if full_system:
        result.append({"role": "system", "content": full_system})

    for msg in messages:
        if msg.role == "tool":
            if isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        result.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": _stringify_tool_result(block.get("content", "")),
                        })
                    else:
                        result.append({"role": "user", "content": str(block)})
            else:
                result.append({"role": "tool", "tool_call_id": "", "content": str(msg.content)})
        elif msg.role == "assistant":
            if isinstance(msg.content, list):
                text_parts = []
                tool_calls_api = []
                reasoning_content_str = ""
                for block in msg.content:
                    if isinstance(block, dict):
                        btype = block.get("type")
                        if btype == "tool_use":
                            tool_calls_api.append({
                                "id": block["id"],
                                "type": "function",
                                "function": {
                                    "name": block["name"],
                                    "arguments": json.dumps(block.get("input", {})),
                                },
                            })
                        elif btype == "text":
                            text_parts.append(block.get("text", ""))
                        elif btype == "reasoning_content":
                            # Moonshot / Kimi-style reasoning captured on a
                            # previous turn. Kimi's API validates that every
                            # assistant message carrying ``tool_calls``
                            # replays it on subsequent requests when thinking
                            # is enabled; losing it yields a 400 on iteration
                            # 2. Only the last reasoning block per turn is
                            # kept — Kimi's API expects a single string.
                            reasoning_content_str = block.get("text", "")
                        # Provider-specific blocks (e.g. Codex "reasoning") are
                        # skipped — they don't round-trip through Chat Completions.
                    else:
                        text_parts.append(str(block))
                text = "".join(text_parts)
                # If the only blocks were provider-opaque (e.g. Codex
                # reasoning) the assistant turn filters down to empty text +
                # no tool_calls. Chat Completions rejects a message with
                # both `content=None` and no `tool_calls`, so substitute a
                # minimal placeholder to keep the turn valid.
                if not text and not tool_calls_api:
                    text = "[continued]"
                entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": text if text else None,
                }
                if tool_calls_api:
                    entry["tool_calls"] = tool_calls_api
                    # Stamp reasoning_content only on tool-call turns: that
                    # is where Kimi's API actually requires it, and standard
                    # OpenAI models would reject an unknown field. Plain-text
                    # turns get no reasoning_content even if captured so a
                    # mixed-provider history (Kimi reasoning echoed to plain
                    # OpenAI) does not 400.
                    if reasoning_content_str:
                        entry["reasoning_content"] = reasoning_content_str
                result.append(entry)
            else:
                result.append({"role": "assistant", "content": msg.content})
        else:
            if isinstance(msg.content, list):
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


# Kept for backwards-compat test imports; delegates to the shared helper
# so rendering is byte-for-byte identical to codex / openai_responses.
_stringify_tool_result = stringify_tool_result_content


def _tool_to_openai(tool: "Tool") -> dict[str, Any]:
    """Convert a butterfly Tool to OpenAI function-calling (Chat Completions) format."""
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
    *,
    extract_usage: Callable[[Any], TokenUsage] = None,  # type: ignore[assignment]
) -> tuple[str, list[ToolCall], TokenUsage]:
    """Parse a non-streaming ChatCompletion response.

    ``extract_usage`` is injectable so subclasses (e.g. KimiOpenAIProvider)
    can widen the usage-extraction logic without reimplementing this parser.
    Defaults to the module-level OpenAI extractor.
    """
    if extract_usage is None:
        extract_usage = _extract_usage_from_obj

    choice = response.choices[0] if response.choices else None
    text = ""
    tool_calls: list[ToolCall] = []

    if choice and choice.message:
        msg = choice.message
        text = msg.content or ""
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        input=_parse_json_args(tc.function.arguments or ""),
                    )
                )

    usage = TokenUsage()
    if response.usage:
        usage = extract_usage(response.usage)

    return text, tool_calls, usage


def _chunk_usage(chunk: Any) -> Any | None:
    """Extract a usage object from a streaming chunk if present.

    OpenAI-proper puts usage on ``chunk.usage``. Some OpenAI-compatible
    gateways (notably Moonshot Kimi) attach usage to the first choice
    (``chunk.choices[0].usage``) instead. This helper normalizes both
    positions so the streaming loop sees a single API.
    """
    top = getattr(chunk, "usage", None)
    if top is not None:
        return top
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return None
    return getattr(choices[0], "usage", None)


def _extract_usage_from_obj(usage: Any) -> TokenUsage:
    """Extract TokenUsage from an OpenAI usage object."""
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(prompt_details, "cached_tokens", 0) if prompt_details else 0
    cached = cached or 0

    completion_details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(completion_details, "reasoning_tokens", 0) if completion_details else 0
    reasoning = reasoning or 0

    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    return TokenUsage(
        input_tokens=max(prompt_tokens - cached, 0),
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        cache_read_tokens=cached,
        cache_write_tokens=0,
        reasoning_tokens=reasoning,
    )


def _tc_map_to_list(tc_map: dict[int, dict[str, Any]]) -> list[ToolCall]:
    """Convert accumulated streaming tool-call fragments to ToolCall list.

    Skips entries where the name never arrived — matches the filter in the
    Codex and OpenAI Responses providers, so a malformed stream doesn't
    surface an unnamed ``ToolCall`` downstream.
    """
    result: list[ToolCall] = []
    for idx in sorted(tc_map):
        entry = tc_map[idx]
        if not entry["name"]:
            continue
        result.append(
            ToolCall(
                id=entry["id"],
                name=entry["name"],
                input=_parse_json_args(entry["arguments"]),
            )
        )
    return result


def _maybe_raise_mapped_openai_error(exc: BaseException) -> None:
    """Translate an openai-SDK exception into the butterfly error taxonomy.

    The OpenAI SDK raises types like ``openai.RateLimitError``,
    ``AuthenticationError``, ``APIConnectionError`` etc. Callers wrap
    ``complete`` in ``try/except`` and pass the raised exception in; we
    inspect status + type + message, raise the mapped butterfly error when
    recognized, and return silently (so the caller re-raises the original)
    for unrecognized cases. No-op on BaseException subclasses like
    ``KeyboardInterrupt`` so cancellation still propagates.
    """
    import asyncio

    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        return

    exc_name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    message = str(exc) or exc_name

    if exc_name in ("AuthenticationError", "PermissionDeniedError") or status in (401, 403):
        raise AuthError(f"OpenAI auth error: {message}", provider="openai", status=status) from exc
    if exc_name == "RateLimitError" or status == 429:
        raise RateLimitError(
            f"OpenAI rate limit: {message}", provider="openai", status=status
        ) from exc
    lowered = message.lower()
    if "context_length_exceeded" in lowered or "maximum context length" in lowered:
        raise ContextWindowExceededError(
            f"OpenAI context length exceeded: {message}", provider="openai", status=status
        ) from exc
    if exc_name == "BadRequestError" or status == 400:
        raise BadRequestError(
            f"OpenAI bad request: {message}", provider="openai", status=status
        ) from exc
    if exc_name in ("APITimeoutError",) or "timeout" in lowered:
        raise ProviderTimeoutError(
            f"OpenAI request timed out: {message}", provider="openai", status=status
        ) from exc
    if status and 500 <= status < 600:
        raise ServerError(
            f"OpenAI server error: {message}", provider="openai", status=status
        ) from exc
    if exc_name in ("APIConnectionError",):
        raise ProviderError(
            f"OpenAI connection error: {message}", provider="openai", status=status
        ) from exc
    # Unrecognized — caller re-raises the original exception.
