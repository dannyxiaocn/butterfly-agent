from __future__ import annotations
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Callable

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
    ) -> tuple[str, list["ToolCall"]]:
        """Send messages to the LLM and return (content, tool_calls).

        Args:
            on_text_chunk: Optional callback invoked with each streamed text chunk.
                           If provided, the provider should use streaming mode.

        Returns a tuple of:
          - content: the assistant's text response (may be empty if tool_calls)
          - tool_calls: list of ToolCall objects (may be empty)
        """
        ...


__all__ = ["Provider"]
