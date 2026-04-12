"""ToolLoader — discovers and loads tools from toolhub/ and session-local tools.

Tool discovery:
  1. Read tool.md (list of enabled tool names, one per line)
  2. For each name, load schema from toolhub/<name>/tool.json
  3. Dynamically import executor from toolhub/<name>/executor.py
  4. Also load agent-created tools from core/tools/ (.json + .sh pairs)

Special tools:
  - reload_capabilities: system tool, injected by session.py (not in toolhub)
  - skill: executor needs skills list injection
  - bash: executor needs workdir injection
  - manage_task: executor needs tasks_dir injection
  - recall_memory: executor needs memory_dir injection
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable

from nutshell.core.skill import Skill
from nutshell.core.tool import Tool


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TOOLHUB_DIR = _REPO_ROOT / "toolhub"


def _load_executor_module(tool_name: str, toolhub_dir: Path | None = None):
    """Dynamically import toolhub/<name>/executor.py and return the module."""
    hub = toolhub_dir or _TOOLHUB_DIR
    executor_path = hub / tool_name / "executor.py"
    if not executor_path.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        f"toolhub_{tool_name}_executor", executor_path
    )
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_tool_schema(tool_name: str, toolhub_dir: Path | None = None) -> dict | None:
    """Load tool.json from toolhub/<name>/tool.json."""
    hub = toolhub_dir or _TOOLHUB_DIR
    schema_path = hub / tool_name / "tool.json"
    if not schema_path.exists():
        return None
    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_tool_md(path: Path) -> list[str]:
    """Read tool.md and return list of tool names (one per line, stripped, no blanks)."""
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class ToolLoader:
    """Load tools from toolhub and session-local directories.

    Args:
        default_workdir: Default working directory for bash/shell executors.
        skills: List of Skill objects for the skill executor.
        tasks_dir: Path to core/tasks/ for manage_task tool.
        memory_dir: Path to core/memory/ for recall_memory tool.
        toolhub_dir: Override toolhub directory (for testing).
    """

    def __init__(
        self,
        default_workdir: str | None = None,
        skills: list[Skill] | None = None,
        tasks_dir: Path | None = None,
        memory_dir: Path | None = None,
        toolhub_dir: Path | None = None,
        # Legacy compatibility
        impl_registry: dict[str, Callable] | None = None,
    ) -> None:
        self._default_workdir = default_workdir
        self._skills = list(skills or [])
        self._tasks_dir = tasks_dir
        self._memory_dir = memory_dir
        self._toolhub_dir = toolhub_dir or _TOOLHUB_DIR
        self._impl_registry = impl_registry or {}

    def _create_executor(self, tool_name: str) -> Callable | None:
        """Create an executor callable for a toolhub tool."""
        mod = _load_executor_module(tool_name, self._toolhub_dir)
        if mod is None:
            return None

        # Tool-specific executor instantiation with context injection
        if tool_name == "bash":
            executor_cls = getattr(mod, "BashExecutor", None)
            if executor_cls:
                executor = executor_cls(workdir=self._default_workdir)
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "skill":
            executor_cls = getattr(mod, "SkillExecutor", None)
            if executor_cls:
                executor = executor_cls(skills=self._skills)
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "web_search":
            executor_cls = getattr(mod, "WebSearchExecutor", None)
            if executor_cls:
                executor = executor_cls()
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "manage_task":
            executor_cls = getattr(mod, "ManageTaskExecutor", None)
            if executor_cls:
                executor = executor_cls(tasks_dir=self._tasks_dir)
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "recall_memory":
            executor_cls = getattr(mod, "RecallMemoryExecutor", None)
            if executor_cls:
                executor = executor_cls(memory_dir=self._memory_dir)
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        # Generic: look for an Executor class or execute function
        executor_cls = getattr(mod, "Executor", None)
        if executor_cls:
            executor = executor_cls()
            async def _impl(**kwargs: Any) -> str:
                return await executor.execute(**kwargs)
            return _impl

        execute_fn = getattr(mod, "execute", None)
        if execute_fn:
            return execute_fn

        return None

    def load_from_toolhub(self, tool_name: str) -> Tool | None:
        """Load a single tool from toolhub by name."""
        schema_data = _load_tool_schema(tool_name, self._toolhub_dir)
        if schema_data is None:
            return None

        name = schema_data.get("name") or tool_name
        description = schema_data.get("description") or ""
        input_schema = schema_data.get("input_schema") or {
            "type": "object", "properties": {}, "required": []
        }

        impl = self._create_executor(tool_name)
        if impl is None:
            async def _stub(**kwargs: Any) -> str:
                raise NotImplementedError(f"Tool '{name}' has no executor in toolhub.")
            impl = _stub

        return Tool(name=name, description=description, func=impl, schema=input_schema)

    def load_from_tool_md(self, tool_md_path: Path) -> list[Tool]:
        """Load all tools listed in a tool.md file."""
        names = _read_tool_md(tool_md_path)
        tools = []
        for name in names:
            tool = self.load_from_toolhub(name)
            if tool is not None:
                tools.append(tool)
            else:
                print(f"[tool_engine] Warning: tool '{name}' not found in toolhub")
        return tools

    def load_local_tools(self, tools_dir: Path) -> list[Tool]:
        """Load agent-created tools from a session's core/tools/ directory.

        Agent-created tools are .json + .sh pairs. The .sh script receives
        all kwargs as JSON on stdin and writes its result to stdout.
        """
        from nutshell.tool_engine.executor.terminal.shell_terminal import ShellExecutor

        if not tools_dir.is_dir():
            return []

        tools = []
        for json_path in sorted(tools_dir.glob("*.json")):
            sh_path = json_path.with_suffix(".sh")
            if not sh_path.exists():
                continue  # Only load .json files that have a matching .sh

            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception:
                continue

            name = data.get("name") or json_path.stem
            description = data.get("description") or ""
            schema = data.get("input_schema") or {"type": "object", "properties": {}, "required": []}

            executor = ShellExecutor(sh_path, cwd=self._default_workdir)
            async def _shell_impl(_ex=executor, **kwargs: Any) -> str:
                return await _ex.execute(**kwargs)

            tools.append(Tool(name=name, description=description, func=_shell_impl, schema=schema))

        return tools

    # Legacy compatibility methods
    def load(self, path: Path) -> Tool:
        """Legacy: load a single tool from a JSON file path."""
        data = json.loads(path.read_text(encoding="utf-8"))
        name = data.get("name") or path.stem
        # Try toolhub first
        tool = self.load_from_toolhub(name)
        if tool:
            return tool
        # Fallback to old behavior for shell tools
        from nutshell.tool_engine.executor.terminal.shell_terminal import ShellExecutor
        description = data.get("description") or ""
        schema = data.get("input_schema") or {"type": "object", "properties": {}, "required": []}
        sh_path = path.with_suffix(".sh")
        if sh_path.exists():
            executor = ShellExecutor(sh_path, cwd=self._default_workdir)
            async def _impl(**kwargs: Any) -> str:
                return await executor.execute(**kwargs)
            return Tool(name=name, description=description, func=_impl, schema=schema)
        async def _stub(**kwargs: Any) -> str:
            raise NotImplementedError(f"Tool '{name}' has no implementation.")
        return Tool(name=name, description=description, func=_stub, schema=schema)

    def load_dir(self, directory: Path) -> list[Tool]:
        """Legacy: load all tools from a directory of .json files."""
        directory = Path(directory)
        if not directory.is_dir():
            return []
        return [self.load(p) for p in sorted(directory.glob("*.json"))]
