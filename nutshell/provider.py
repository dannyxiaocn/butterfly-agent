from __future__ import annotations
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nutshell.types import Message, ToolCall, AgentResult
    from nutshell.tool import Tool


class Provider(ABC):
    """Abstract LLM provider interface."""

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[Tool],
        system_prompt: str,
        model: str,
    ) -> tuple[str, list[ToolCall]]:
        """Send messages to the LLM and return (content, tool_calls).

        Returns a tuple of:
          - content: the assistant's text response (may be empty if tool_calls)
          - tool_calls: list of ToolCall objects (may be empty)
        """
        ...
