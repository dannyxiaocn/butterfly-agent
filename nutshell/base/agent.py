from __future__ import annotations
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nutshell.types import AgentResult


class BaseAgent(ABC):
    """Abstract interface for an agent that processes messages."""

    @abstractmethod
    async def run(self, input: str, *, clear_history: bool = False) -> "AgentResult":
        """Run the agent with a user input string and return a result."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Release any held state (e.g., conversation history)."""
        ...
