from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class BaseExecutor(ABC):
    """Abstract base for tool executors."""

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str: ...
