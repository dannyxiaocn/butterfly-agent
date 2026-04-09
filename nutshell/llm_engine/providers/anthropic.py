from __future__ import annotations
import importlib.util
import os
from typing import TYPE_CHECKING, Any, Callable, ClassVar

from nutshell.core.provider import Provider
from nutshell.core.types import Message, TokenUsage, ToolCall

if TYPE_CHECKING:
    from nutshell.core.tool import Tool


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
        self._client = _anthropic.AsyncAnthropic(**client_kwargs)
        self.max_tokens = max_tokens

    async def complete(
        self,
        messages: list[Message],
        tools: list["Tool"],
        system_prompt: str,
        model: str,
        *,
        on_text_chunk: Callable[[str], None] | None = None,
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

        saw_streamed_thinking = False
        if on_text_chunk is not None:
            stream_kwargs = {"betas": betas, **kwargs} if betas else kwargs
            async with messages_ns.stream(**stream_kwargs) as stream:
                async for event in stream:
                    if _forward_stream_event(event, on_text_chunk):
                        saw_streamed_thinking = True
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
                thinking_text = _extract_thinking_text(block)
                if thinking_text and on_text_chunk is not None and not saw_streamed_thinking:
                    on_text_chunk(thinking_text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=block.input))

        usage = _extract_usage(response)
        return content_text, tool_calls, usage


def _forward_stream_event(event: Any, on_text_chunk: Callable[[str], None]) -> bool:
    if getattr(event, "type", None) != "content_block_delta":
        return False

    delta = getattr(event, "delta", None)
    delta_type = getattr(delta, "type", None)
    if delta_type == "text_delta":
        text = getattr(delta, "text", None)
        if text:
            on_text_chunk(text)
        return False
    if delta_type == "thinking_delta":
        thinking = getattr(delta, "thinking", None)
        if thinking:
            on_text_chunk(thinking)
            return True
    return False


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
    return TokenUsage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
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


def _to_api_messages(
    messages: list[Message],
    cache_breakpoint_index: int | None = None,
) -> list[dict]:
    result = []
    for i, msg in enumerate(messages):
        role = "user" if msg.role == "tool" else msg.role
        content = msg.content

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
