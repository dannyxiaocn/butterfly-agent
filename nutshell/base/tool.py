from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class BaseTool(ABC):
    """Abstract interface for a tool an agent can call.

    Concrete subclasses must set instance attributes:
        name: str        — unique tool identifier
        description: str — human/LLM-readable description
        schema: dict     — JSON Schema for the tool's input parameters
    """

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """Execute the tool and return a string result."""
        ...

    @abstractmethod
    def to_api_dict(self) -> dict:
        """Return the LLM API representation (Anthropic input_schema format)."""
        ...
