from __future__ import annotations
import asyncio
import json
from pathlib import Path
from typing import Any, Callable

from nutshell.runtime.loaders import BaseLoader
from nutshell.core.tool import Tool


def _make_stub(name: str) -> Callable:
    """Return a placeholder async function for tools without a registered implementation."""
    async def _stub(**kwargs: Any) -> str:
        raise NotImplementedError(
            f"Tool '{name}' has no registered Python implementation. "
            "Pass impl_registry to ToolLoader or call loader.register()."
        )
    _stub.__name__ = name
    return _stub


def _make_shell_impl(sh_path: Path) -> Callable:
    """Return an async impl that runs a shell script, passing kwargs as JSON on stdin.

    The script receives all tool arguments as a JSON object on stdin and should
    write its result to stdout. Non-zero exit code returns stderr as an error string.
    """
    sh_path_str = str(sh_path)

    async def _shell_tool(**kwargs: Any) -> str:
        input_json = json.dumps(kwargs).encode()
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", sh_path_str,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_json),
                timeout=30.0,
            )
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace")
                return f"Error (exit {proc.returncode}): {err[:500]}"
            return stdout.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            return "Error: shell tool timed out after 30s"
        except Exception as e:
            return f"Error: {e}"

    _shell_tool.__name__ = sh_path.stem
    return _shell_tool


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

        if name in self._registry:
            impl = self._registry[name]
        else:
            from nutshell.runtime.tools._registry import get_builtin
            impl = get_builtin(name)
            if impl is None:
                sh_path = path.with_suffix(".sh")
                impl = _make_shell_impl(sh_path) if sh_path.exists() else _make_stub(name)
        return Tool(name=name, description=description, func=impl, schema=schema)

    def load_dir(self, directory: Path) -> list[Tool]:
        directory = Path(directory)
        return [self.load(p) for p in sorted(directory.glob("*.json"))]
