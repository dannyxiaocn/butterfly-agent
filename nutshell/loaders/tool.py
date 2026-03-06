from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Callable

from nutshell.base.loader import BaseLoader
from nutshell.tool import Tool


def _make_stub(name: str) -> Callable:
    """Return a placeholder async function for tools without a registered implementation."""
    async def _stub(**kwargs: Any) -> str:
        raise NotImplementedError(
            f"Tool '{name}' has no registered Python implementation. "
            "Pass impl_registry to ToolLoader or call loader.register()."
        )
    _stub.__name__ = name
    return _stub


class ToolLoader(BaseLoader[Tool]):
    """Load JSON Schema tool definition files as Tool objects.

    The JSON file defines schema/metadata; Python implementations are
    provided separately via impl_registry or register().

    File format (Anthropic-compatible JSON Schema):
        {
          "name": "tool_name",
          "description": "...",
          "input_schema": {
            "type": "object",
            "properties": { ... },
            "required": [...]
          }
        }

    Args:
        impl_registry: Optional dict mapping tool name -> callable.
    """

    def __init__(self, impl_registry: dict[str, Callable] | None = None) -> None:
        self._registry: dict[str, Callable] = impl_registry or {}

    def register(self, name: str, func: Callable) -> None:
        """Register a Python implementation for a tool by name."""
        self._registry[name] = func

    def load(self, path: Path) -> Tool:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Tool definition file not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))

        name: str = data.get("name") or path.stem
        description: str = data.get("description") or ""
        schema: dict = data.get("input_schema") or data.get("schema") or {
            "type": "object", "properties": {}, "required": []
        }

        impl = self._registry.get(name) or _make_stub(name)
        return Tool(name=name, description=description, func=impl, schema=schema)

    def load_dir(self, directory: Path) -> list[Tool]:
        directory = Path(directory)
        return [self.load(p) for p in sorted(directory.glob("*.json"))]
