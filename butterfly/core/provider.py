from __future__ import annotations
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Callable

from butterfly.core.types import TokenUsage

if TYPE_CHECKING:
    from butterfly.core.types import Message, ToolCall
    from butterfly.core.tool import Tool


class Provider(ABC):
    """Abstract LLM provider interface."""

    @abstractmethod
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
    ) -> "tuple[str, list[ToolCall], TokenUsage]":
        """Send messages to the LLM and return (content, tool_calls, usage).

        Args:
            on_text_chunk: Optional callback invoked with each streamed text chunk.
                           If provided, the provider should use streaming mode.

        Returns a tuple of:
          - content: the assistant's text response (may be empty if tool_calls)
          - tool_calls: list of ToolCall objects (may be empty)
          - usage: TokenUsage with input/output/cache token counts
        """
        ...

    def consume_extra_blocks(self) -> list[dict]:
        """Return provider-generated content blocks to attach to the last assistant Message.

        The agent loop appends these verbatim to the assistant message content so
        they round-trip to the provider on the next turn. Used by providers whose
        server-side state must be re-echoed (e.g. OpenAI Responses reasoning items
        with encrypted_content). Default: no extra blocks.
        """
        return []


__all__ = ["Provider"]
