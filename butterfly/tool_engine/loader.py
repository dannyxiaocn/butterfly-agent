"""ToolLoader — discovers and loads tools from toolhub/ and session-local tools.

Tool discovery:
  1. Read tools.md (list of enabled tool names, one per line)
  2. For each name, load schema from toolhub/<name>/tool.json
  3. Dynamically import executor from toolhub/<name>/executor.py
  4. Also load agent-created tools from core/tools/ (.json + .sh pairs)

Context injection — executors receive injected context so the agent passes
only business-intent parameters:
  - bash: workdir + tool_results_dir (for disk spillover)
  - session_shell: workdir + venv_env_provider
  - read/write/edit/glob/grep: workdir
  - task_*: tasks_dir
  - recall_memory: memory_dir
  - update_memory: memory_dir + main_memory_path
  - tool_output: panel_dir
  - skill: skills list
  - web_search / web_fetch: no injection (provider config via registry)
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable

from butterfly.core.guardian import Guardian
from butterfly.core.skill import Skill
from butterfly.core.tool import Tool


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
    """Read tools.md and return list of tool names (one per line, stripped, no blanks)."""
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
        tasks_dir: Path to core/tasks/ for task_* tools.
        memory_dir: Path to core/memory/ for recall_memory tool.
        toolhub_dir: Override toolhub directory (for testing).
    """

    def __init__(
        self,
        default_workdir: str | None = None,
        skills: list[Skill] | None = None,
        tasks_dir: Path | None = None,
        memory_dir: Path | None = None,
        main_memory_path: Path | None = None,
        panel_dir: Path | None = None,
        tool_results_dir: Path | None = None,
        toolhub_dir: Path | None = None,
        # Legacy compatibility
        impl_registry: dict[str, Callable] | None = None,
        # Guardian — when set, write/edit/bash run inside a path boundary
        # (used by sub-agent's explorer mode).
        guardian: "Guardian | None" = None,
        # Sub-agent context — needed by toolhub/sub_agent so the spawned child
        # lands in the right sessions/_sessions trees and is recorded as a
        # child of the calling session.
        parent_session_id: str | None = None,
        sessions_base: Path | None = None,
        system_sessions_base: Path | None = None,
        entity_base: Path | None = None,
    ) -> None:
        self._default_workdir = default_workdir
        self._skills = list(skills or [])
        self._tasks_dir = tasks_dir
        self._memory_dir = memory_dir
        self._main_memory_path = main_memory_path
        self._panel_dir = panel_dir
        self._tool_results_dir = tool_results_dir
        self._toolhub_dir = toolhub_dir or _TOOLHUB_DIR
        self._impl_registry = impl_registry or {}
        self._guardian = guardian
        self._parent_session_id = parent_session_id
        self._sessions_base = sessions_base
        self._system_sessions_base = system_sessions_base
        self._entity_base = entity_base

    def _create_executor(self, tool_name: str) -> Callable | None:
        """Create an executor callable for a toolhub tool."""
        # Check impl_registry first (allows callers to override toolhub executors)
        if tool_name in self._impl_registry:
            return self._impl_registry[tool_name]

        mod = _load_executor_module(tool_name, self._toolhub_dir)
        if mod is None:
            return None

        # Tool-specific executor instantiation with context injection
        if tool_name == "bash":
            executor_cls = getattr(mod, "BashExecutor", None)
            if executor_cls:
                executor = executor_cls(
                    workdir=self._default_workdir,
                    tool_results_dir=self._tool_results_dir,
                    guardian=self._guardian,
                )
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "tool_output":
            executor_cls = getattr(mod, "ToolOutputExecutor", None)
            if executor_cls:
                executor = executor_cls(
                    panel_dir=self._panel_dir,
                    tool_results_dir=self._tool_results_dir,
                )
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "update_memory":
            executor_cls = getattr(mod, "UpdateMemoryExecutor", None)
            if executor_cls:
                executor = executor_cls(
                    memory_dir=self._memory_dir,
                    main_memory_path=self._main_memory_path,
                )
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "session_shell":
            executor_cls = getattr(mod, "SessionShellExecutor", None)
            if executor_cls:
                # Mirror bash_terminal._venv_env() as a callable so the
                # executor can pick up the session venv on first spawn.
                def _venv_env_provider() -> dict[str, str] | None:
                    try:
                        from butterfly.tool_engine.executor.terminal.bash_terminal import (
                            _venv_env,
                        )
                        return _venv_env()
                    except Exception:
                        return None

                executor = executor_cls(
                    workdir=self._default_workdir,
                    venv_env_provider=_venv_env_provider,
                )
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "sub_agent":
            executor_cls = getattr(mod, "SubAgentTool", None)
            if executor_cls:
                executor = executor_cls(
                    parent_session_id=self._parent_session_id,
                    sessions_base=self._sessions_base,
                    system_sessions_base=self._system_sessions_base,
                    entity_base=self._entity_base,
                )
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

        elif tool_name == "web_fetch":
            executor_cls = getattr(mod, "WebFetchExecutor", None)
            if executor_cls:
                executor = executor_cls()
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "task_create":
            executor_cls = getattr(mod, "TaskCreateExecutor", None)
            if executor_cls:
                executor = executor_cls(tasks_dir=self._tasks_dir)
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "task_update":
            executor_cls = getattr(mod, "TaskUpdateExecutor", None)
            if executor_cls:
                executor = executor_cls(tasks_dir=self._tasks_dir)
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "task_finish":
            executor_cls = getattr(mod, "TaskFinishExecutor", None)
            if executor_cls:
                executor = executor_cls(tasks_dir=self._tasks_dir)
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "task_pause":
            executor_cls = getattr(mod, "TaskPauseExecutor", None)
            if executor_cls:
                executor = executor_cls(tasks_dir=self._tasks_dir)
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "task_resume":
            executor_cls = getattr(mod, "TaskResumeExecutor", None)
            if executor_cls:
                executor = executor_cls(tasks_dir=self._tasks_dir)
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "task_list":
            executor_cls = getattr(mod, "TaskListExecutor", None)
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

        elif tool_name == "read":
            executor_cls = getattr(mod, "ReadExecutor", None)
            if executor_cls:
                executor = executor_cls(workdir=self._default_workdir)
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "write":
            executor_cls = getattr(mod, "WriteExecutor", None)
            if executor_cls:
                executor = executor_cls(
                    workdir=self._default_workdir,
                    guardian=self._guardian,
                )
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "edit":
            executor_cls = getattr(mod, "EditExecutor", None)
            if executor_cls:
                executor = executor_cls(
                    workdir=self._default_workdir,
                    guardian=self._guardian,
                )
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "glob":
            executor_cls = getattr(mod, "GlobExecutor", None)
            if executor_cls:
                executor = executor_cls(workdir=self._default_workdir)
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "grep":
            executor_cls = getattr(mod, "GrepExecutor", None)
            if executor_cls:
                executor = executor_cls(workdir=self._default_workdir)
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

        backgroundable = bool(schema_data.get("backgroundable", False))
        return Tool(
            name=name,
            description=description,
            func=impl,
            schema=input_schema,
            backgroundable=backgroundable,
        )

    def load_from_tool_md(self, tool_md_path: Path) -> list[Tool]:
        """Load all tools listed in a tools.md file."""
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
        from butterfly.tool_engine.executor.terminal.shell_terminal import ShellExecutor

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
        from butterfly.tool_engine.executor.terminal.shell_terminal import ShellExecutor
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
