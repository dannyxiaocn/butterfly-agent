from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from nutshell.core.tool import Tool
from nutshell.core.loader import BaseLoader
from nutshell.tool_engine.executor.bash import BashExecutor
from nutshell.tool_engine.executor.shell import ShellExecutor


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

    Tool taxonomy
    -------------
    System tools (bash, web_search):
        Python-implemented, registered in tool_engine/registry.py.
        Not agent-creatable; no hot-swap.  Resolved via impl_registry or
        BashExecutor / get_builtin().

    Agent-created tools:
        Always shell-backed (.json + .sh pair).  The .sh script receives
        all kwargs as JSON on stdin and writes its result to stdout.
        Agents may call Python (or any interpreter) inside the script —
        it is still a shell tool from the system's perspective.
        Hot-reloaded via reload_capabilities.

    Resolution chain (highest priority first):
      1. impl_registry (caller-supplied callables) — system tools
      2. BashExecutor  — tool_name == "bash"
      3. ShellExecutor — sibling .sh file exists (agent-created tools)
      4. get_builtin() — remaining system built-ins (web_search, …)
      5. Stub that raises NotImplementedError

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

    def __init__(
        self,
        impl_registry: dict[str, Callable] | None = None,
        default_workdir: str | None = None,
    ) -> None:
        self._registry: dict[str, Callable] = impl_registry or {}
        self._default_workdir = default_workdir

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

        # Resolution chain
        if name in self._registry:
            impl = self._registry[name]
        elif BashExecutor.can_handle(name, path):
            executor = BashExecutor(workdir=self._default_workdir)
            async def _bash_impl(**kwargs: Any) -> str:
                return await executor.execute(**kwargs)
            _bash_impl.__name__ = "bash"
            impl = _bash_impl
        elif ShellExecutor.can_handle(name, path):
            executor = ShellExecutor(path.with_suffix(".sh"), cwd=self._default_workdir)
            async def _shell_impl(**kwargs: Any) -> str:
                return await executor.execute(**kwargs)
            _shell_impl.__name__ = name
            impl = _shell_impl
        else:
            # Check built-in registry as fallback before stub
            from nutshell.tool_engine.registry import get_builtin
            impl = get_builtin(name)
            if impl is None:
                impl = _make_stub(name)

        return Tool(name=name, description=description, func=impl, schema=schema)

    def load_dir(self, directory: Path) -> list[Tool]:
        directory = Path(directory)
        return [self.load(p) for p in sorted(directory.glob("*.json"))]
