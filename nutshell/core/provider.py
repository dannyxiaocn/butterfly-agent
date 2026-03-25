from __future__ import annotations
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Callable

from nutshell.core.types import TokenUsage

if TYPE_CHECKING:
    from nutshell.core.types import Message, ToolCall
    from nutshell.core.tool import Tool


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


__all__ = ["Provider"]
