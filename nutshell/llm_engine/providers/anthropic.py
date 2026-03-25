from __future__ import annotations
from typing import TYPE_CHECKING, Any, Callable, ClassVar

from nutshell.core.provider import Provider
from nutshell.core.types import Message, ToolCall

if TYPE_CHECKING:
    from nutshell.core.tool import Tool


class AnthropicProvider(Provider):
    """LLM provider backed by Anthropic Claude."""

    _supports_cache_control: ClassVar[bool] = True

    def __init__(
        self,
        api_key: str | None = None,
        max_tokens: int = 8096,
        base_url: str | None = None,
    ) -> None:
        try:
            import anthropic as _anthropic
        except ImportError:
            raise ImportError("Install anthropic: pip install anthropic") from None
        self._client = _anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
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
    ) -> tuple[str, list[ToolCall]]:
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
        if api_tools:
            kwargs["tools"] = api_tools

        saw_streamed_thinking = False
        if on_text_chunk is not None:
            async with self._client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if _forward_stream_event(event, on_text_chunk):
                        saw_streamed_thinking = True
                response = await stream.get_final_message()
        else:
            response = await self._client.messages.create(**kwargs)

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

        return content_text, tool_calls


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
