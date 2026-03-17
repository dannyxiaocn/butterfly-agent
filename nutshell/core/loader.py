from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Generic, TypeVar

T = TypeVar("T")


class BaseLoader(ABC, Generic[T]):
    """Abstract base for loaders that read external files into nutshell objects."""

    @abstractmethod
    def load(self, path: Path) -> T: ...

    @abstractmethod
    def load_dir(self, directory: Path) -> list[T]: ...


__all__ = ["BaseLoader"]
