from __future__ import annotations
from abc import ABC, abstractmethod


class BaseSkill(ABC):
    """Abstract interface for a skill that injects behavior into a system prompt."""

    @abstractmethod
    def to_prompt_fragment(self) -> str:
        """Return the string fragment to append to the system prompt."""
        ...
