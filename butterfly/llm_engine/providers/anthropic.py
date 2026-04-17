from __future__ import annotations
import importlib.util
import os
from typing import TYPE_CHECKING, Any, Callable, ClassVar

from butterfly.core.provider import Provider
from butterfly.core.types import Message, TokenUsage, ToolCall

if TYPE_CHECKING:
    from butterfly.core.tool import Tool


class AnthropicProvider(Provider):
    _supports_thinking: ClassVar[bool] = True
    """LLM provider backed by Anthropic Claude."""

    _supports_cache_control: ClassVar[bool] = True
    # When True, thinking is enabled via Anthropic's betas header + thinking param.
    # When False (e.g. Kimi), thinking is enabled via extra_body only (no betas).
    _thinking_uses_betas: ClassVar[bool] = True

    def __init__(
        self,
        api_key: str | None = None,
        max_tokens: int = 8096,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        try:
            import anthropic as _anthropic
            import httpx
        except ImportError:
            raise ImportError("Install anthropic: pip install anthropic") from None
        http_client = _build_http_client(httpx)
        client_kwargs: dict[str, Any] = {"api_key": api_key, "base_url": base_url}
        if http_client is not None:
            client_kwargs["http_client"] = http_client
        if default_headers is not None:
            client_kwargs["default_headers"] = default_headers
        self._client = _anthropic.AsyncAnthropic(**client_kwargs)
        self.max_tokens = max_tokens

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result

    async def complete(
        self,
        messages: list[Message],
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
        thinking_budget: int = 8000,
        thinking_effort: str = "high",  # ignored — Anthropic uses budget_tokens, not effort
    ) -> tuple[str, list[ToolCall], TokenUsage]:
        cache_idx = _find_cache_breakpoint(messages) if (
            cache_last_human_turn and self._supports_cache_control
        ) else None
        api_messages = _to_api_messages(messages, cache_breakpoint_index=cache_idx)
        api_tools = [t.to_api_dict() for t in tools] if tools else []

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "system": _build_system_param(cache_system_prefix, system_prompt, self._supports_cache_control),
            "messages": api_messages,
        }
        if thinking and self._supports_thinking:
            if self._thinking_uses_betas:
                kwargs["betas"] = ["interleaved-thinking-2025-05-14"]
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
            else:
                kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
            kwargs["max_tokens"] = max(self.max_tokens, thinking_budget + 1000)
        if api_tools:
            kwargs["tools"] = api_tools

        # beta.messages is required when betas=[...] is set (e.g. interleaved thinking).
        # Regular messages.stream/create does not accept the 'betas' kwarg.
        betas = kwargs.pop("betas", None)
        messages_ns = self._client.beta.messages if betas else self._client.messages

        # Thinking state is accumulated INSIDE the stream path (it's the only
        # place where block-lifecycle events are visible). We never forward
        # thinking text into `on_text_chunk` — that would leak it into the
        # partial_text channel that renders as regular assistant output in the
        # web UI. Instead thinking is delivered through the dedicated
        # on_thinking_start / on_thinking_end hooks so the UI can render a
        # tool-like "💭 Thinking…" cell.
        streamed_thinking_blocks: list[str] = []
        saw_streamed_thinking = False
        if on_text_chunk is not None or on_thinking_start is not None or on_thinking_end is not None:
            stream_kwargs = {"betas": betas, **kwargs} if betas else kwargs
            async with messages_ns.stream(**stream_kwargs) as stream:
                current_thinking_parts: list[str] = []
                thinking_active = False
                async for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "content_block_start":
                        block = getattr(event, "content_block", None)
                        btype = getattr(block, "type", None)
                        if btype in ("thinking", "redacted_thinking"):
                            thinking_active = True
                            current_thinking_parts = []
                            saw_streamed_thinking = True
                            if on_thinking_start is not None:
                                try:
                                    on_thinking_start()
                                except Exception:  # noqa: BLE001 - hook must not crash stream
                                    pass
                    elif etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        dtype = getattr(delta, "type", None)
                        if dtype == "text_delta":
                            text = getattr(delta, "text", None) or ""
                            if text and on_text_chunk is not None:
                                on_text_chunk(text)
                        elif dtype == "thinking_delta":
                            # BUFFER thinking locally; do NOT forward to UI text.
                            part = getattr(delta, "thinking", None) or ""
                            if part:
                                current_thinking_parts.append(part)
                    elif etype == "content_block_stop":
                        if thinking_active:
                            thinking_active = False
                            body = "".join(current_thinking_parts)
                            current_thinking_parts = []
                            streamed_thinking_blocks.append(body)
                            if on_thinking_end is not None:
                                try:
                                    on_thinking_end(body)
                                except Exception:  # noqa: BLE001
                                    pass
                response = await stream.get_final_message()
        else:
            create_kwargs = {"betas": betas, **kwargs} if betas else kwargs
            response = await messages_ns.create(**create_kwargs)

        content_text = ""
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                content_text += block.text
            elif block.type == "thinking":
                # Non-stream path (or stream without start/delta visibility):
                # if we didn't already emit the block via the streaming
                # lifecycle, synthesize a start+end pair now so the UI still
                # renders a thinking cell. Still never forwarded to text_chunk.
                if not saw_streamed_thinking:
                    thinking_text = _extract_thinking_text(block)
                    if on_thinking_start is not None:
                        try:
                            on_thinking_start()
                        except Exception:  # noqa: BLE001
                            pass
                    if on_thinking_end is not None:
                        try:
                            on_thinking_end(thinking_text)
                        except Exception:  # noqa: BLE001
                            pass
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=block.input))

        usage = _extract_usage(response)
        return content_text, tool_calls, usage


def _build_http_client(httpx_module: Any) -> Any | None:
    """Prefer explicit HTTP(S) proxies when SOCKS support is unavailable.

    Some local environments export both HTTP(S)_PROXY and ALL_PROXY=socks5://...
    but do not have socksio installed. httpx then errors before the request is
    sent. When that happens, pin the client to the HTTP(S) proxy explicitly and
    ignore the environment proxy auto-detection.
    """
    https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    all_proxy = os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")

    explicit_proxy = https_proxy or http_proxy
    if not explicit_proxy:
        return None

    if _is_socks_proxy(all_proxy) and not _has_socks_support():
        return httpx_module.AsyncClient(proxy=explicit_proxy, trust_env=False)
    return None


def _is_socks_proxy(proxy_url: str | None) -> bool:
    if not proxy_url:
        return False
    return proxy_url.lower().startswith(("socks4://", "socks4a://", "socks5://", "socks5h://"))


def _has_socks_support() -> bool:
    return importlib.util.find_spec("socksio") is not None


def _extract_usage(response: Any) -> TokenUsage:
    """Extract token usage from an Anthropic API response."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    # Kimi thinking mode reports reasoning_tokens via output_tokens_details.
    # Anthropic proper folds reasoning into output_tokens and doesn't expose
    # this field, in which case the getattr default of 0 is correct.
    out_details = getattr(usage, "output_tokens_details", None)
    reasoning = getattr(out_details, "reasoning_tokens", 0) if out_details else 0
    return TokenUsage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        reasoning_tokens=reasoning or 0,
    )


def _extract_thinking_text(block: Any) -> str:
    thinking = getattr(block, "thinking", None)
    if isinstance(thinking, str):
        return thinking
    text = getattr(block, "text", None)
    if isinstance(text, str):
        return text
    return ""


def _build_system_param(
    cache_prefix: str,
    dynamic: str,
    supports_cache: bool,
) -> str | list[dict]:
    """Build the system param for the Anthropic API.

    When caching is supported and a prefix is provided, returns a list of text
    blocks with cache_control on the prefix. Otherwise returns a plain string.
    """
    if not cache_prefix:
        return dynamic
    if not supports_cache:
        # Concatenate for providers that don't support cache_control
        return (cache_prefix + "\n" + dynamic).strip() if dynamic else cache_prefix
    blocks: list[dict] = [
        {"type": "text", "text": cache_prefix, "cache_control": {"type": "ephemeral"}},
    ]
    if dynamic:
        blocks.append({"type": "text", "text": dynamic})
    return blocks


def _find_cache_breakpoint(messages: list[Message]) -> int | None:
    """Return the index of the last user/human message before the final message.

    This is where we place the cache breakpoint so Anthropic caches all
    conversation history up to (and including) that message on the next call.

    Returns None if there are fewer than 2 messages or no suitable breakpoint.
    """
    if len(messages) < 2:
        return None
    # Walk backwards from second-to-last message, find last non-tool role
    for i in range(len(messages) - 2, -1, -1):
        if messages[i].role in ("user", "assistant"):
            return i
    return None


# Block types the Anthropic Messages API accepts on assistant content. We
# strip anything else (notably "reasoning" from Codex/OpenAI Responses) so
# a cross-provider fallback doesn't send opaque blocks the server rejects.
_ANTHROPIC_ALLOWED_BLOCK_TYPES = frozenset({
    "text", "thinking", "redacted_thinking",
    "tool_use", "tool_result",
    "image", "document",
})


def _sanitize_content_for_anthropic(content: Any) -> Any:
    """Filter assistant content blocks down to Anthropic-accepted types.

    String content is returned as-is. A list with every block stripped
    collapses to a single ``[continued]`` text block so the turn stays
    valid (Anthropic rejects empty content arrays).
    """
    if not isinstance(content, list):
        return content
    filtered = [
        b for b in content
        if not isinstance(b, dict) or b.get("type") in _ANTHROPIC_ALLOWED_BLOCK_TYPES
    ]
    if not filtered:
        return [{"type": "text", "text": "[continued]"}]
    return filtered


def _to_api_messages(
    messages: list[Message],
    cache_breakpoint_index: int | None = None,
) -> list[dict]:
    result = []
    for i, msg in enumerate(messages):
        role = "user" if msg.role == "tool" else msg.role
        # Only sanitize ASSISTANT content — user messages and tool_result
        # payloads may legitimately contain custom block types (images,
        # documents, etc.) that the filter would otherwise strip away. The
        # cross-provider fallback hazard is purely about assistant-origin
        # provider-opaque blocks (e.g. Codex-produced reasoning).
        content = (
            _sanitize_content_for_anthropic(msg.content)
            if msg.role == "assistant"
            else msg.content
        )

        # Add cache_control at the specified breakpoint
        if i == cache_breakpoint_index:
            if isinstance(content, str):
                content = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
            elif isinstance(content, list) and content:
                # Mutate last block in the list to add cache_control
                last = dict(content[-1])
                last["cache_control"] = {"type": "ephemeral"}
                content = [*content[:-1], last]

        result.append({"role": role, "content": content})
    return result
