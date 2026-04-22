"""ToolLoader — discovers and loads tools from toolhub/ and session-local tools.

Tool discovery:
  1. Read tools.md (list of enabled tool names, one per line)
  2. For each name, load schema from toolhub/<name>/tool.json
  3. Dynamically import executor from toolhub/<name>/executor.py
  4. Also load agent-created tools from core/tools/ (.json + .sh pairs)

Context injection — executors receive injected context so the agent passes
only business-intent parameters:
  - bash: workdir + tool_results_dir (for disk spillover)
  - terminal_create / terminal_use: share one TerminalExecutor per session
    (workdir + venv_env_provider + terminal_logger). The loader dispatches
    the two tool names to ``.create()`` / ``.use()`` on the shared singleton.
  - read/write/edit/glob/grep: workdir
  - task_*: tasks_dir
  - memory_recall: memory_dir
  - memory_update: memory_dir + main_memory_path
  - tool_output: panel_dir
  - skill: skills list
  - web_search_brave / web_search_tavily / web_fetch: no injection
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
        memory_dir: Path to core/memory/ for memory_recall tool.
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
        terminal_dir: Path | None = None,
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
        agent_base: Path | None = None,
        # v2.0.30 — task CRUD tools emit a `task_card_changed` event via this
        # callback so the web UI can refresh the Tasks tab on-event. Session
        # provides one that appends to events.jsonl; None is a silent no-op.
        on_task_change: "Callable[[str, str], None] | None" = None,
        # v2.0.30 — session-owned persistent TerminalExecutor driving both
        # ``terminal_create`` and ``terminal_use``. When provided, the
        # loader reuses this instance across capability reloads so the pty
        # stays alive between agent turns. Tests/CLI can leave this None
        # and a per-loader instance is created instead.
        terminal_executor: Any | None = None,
    ) -> None:
        self._default_workdir = default_workdir
        self._skills = list(skills or [])
        self._tasks_dir = tasks_dir
        self._memory_dir = memory_dir
        self._main_memory_path = main_memory_path
        self._panel_dir = panel_dir
        self._terminal_dir = terminal_dir
        self._tool_results_dir = tool_results_dir
        self._toolhub_dir = toolhub_dir or _TOOLHUB_DIR
        self._impl_registry = impl_registry or {}
        self._guardian = guardian
        self._parent_session_id = parent_session_id
        self._sessions_base = sessions_base
        self._system_sessions_base = system_sessions_base
        self._agent_base = agent_base
        self._on_task_change = on_task_change
        self._terminal_executor_override = terminal_executor
        # Populated when the first terminal_create / terminal_use tool is
        # wired; lets the web Terminal route reach the pty directly.
        self._terminal_executor: Any | None = terminal_executor

    def _ensure_terminal_executor(self) -> Any | None:
        """Return the session-scoped TerminalExecutor, building one when
        the test/CLI path didn't inject an override. Cached so
        ``terminal_create`` and ``terminal_use`` pick up the same pty."""
        if self._terminal_executor is not None:
            return self._terminal_executor
        if self._terminal_executor_override is not None:
            self._terminal_executor = self._terminal_executor_override
            return self._terminal_executor
        from butterfly.tool_engine.executor.pure_context.terminal import (
            TerminalExecutor,
        )

        def _venv_env_provider() -> dict[str, str] | None:
            try:
                from butterfly.tool_engine.executor.terminal.bash_terminal import (
                    _venv_env,
                )
                return _venv_env()
            except Exception:
                return None

        terminal_logger = None
        if self._terminal_dir is not None:
            from butterfly.session_engine.terminal import TerminalLogger
            terminal_logger = TerminalLogger(self._terminal_dir)

        self._terminal_executor = TerminalExecutor(
            workdir=self._default_workdir,
            venv_env_provider=_venv_env_provider,
            guardian=self._guardian,
            terminal_logger=terminal_logger,
        )
        return self._terminal_executor

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

        elif tool_name == "memory_update":
            executor_cls = getattr(mod, "MemoryUpdateExecutor", None)
            if executor_cls:
                executor = executor_cls(
                    memory_dir=self._memory_dir,
                    main_memory_path=self._main_memory_path,
                )
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name in ("terminal_create", "terminal_use"):
            # Both verbs share a single TerminalExecutor — lazily create
            # it on the first reference; subsequent lookups reuse.
            executor = self._ensure_terminal_executor()
            if executor is None:
                return None
            if tool_name == "terminal_create":
                async def _impl(**kwargs: Any) -> str:
                    return await executor.create(**kwargs)
            else:
                async def _impl(**kwargs: Any) -> str:
                    return await executor.use(**kwargs)
            return _impl

        elif tool_name == "sub_agent":
            executor_cls = getattr(mod, "SubAgentTool", None)
            if executor_cls:
                executor = executor_cls(
                    parent_session_id=self._parent_session_id,
                    sessions_base=self._sessions_base,
                    system_sessions_base=self._system_sessions_base,
                    agent_base=self._agent_base,
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

        elif tool_name == "web_search_brave":
            executor_cls = getattr(mod, "WebSearchBraveExecutor", None)
            if executor_cls:
                executor = executor_cls()
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "web_search_tavily":
            executor_cls = getattr(mod, "WebSearchTavilyExecutor", None)
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
                executor = executor_cls(
                    tasks_dir=self._tasks_dir,
                    on_change=self._on_task_change,
                )
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "task_update":
            executor_cls = getattr(mod, "TaskUpdateExecutor", None)
            if executor_cls:
                executor = executor_cls(
                    tasks_dir=self._tasks_dir,
                    on_change=self._on_task_change,
                )
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "task_finish":
            executor_cls = getattr(mod, "TaskFinishExecutor", None)
            if executor_cls:
                executor = executor_cls(
                    tasks_dir=self._tasks_dir,
                    on_change=self._on_task_change,
                )
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "task_pause":
            executor_cls = getattr(mod, "TaskPauseExecutor", None)
            if executor_cls:
                executor = executor_cls(
                    tasks_dir=self._tasks_dir,
                    on_change=self._on_task_change,
                )
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "task_resume":
            executor_cls = getattr(mod, "TaskResumeExecutor", None)
            if executor_cls:
                executor = executor_cls(
                    tasks_dir=self._tasks_dir,
                    on_change=self._on_task_change,
                )
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

        elif tool_name == "memory_recall":
            executor_cls = getattr(mod, "MemoryRecallExecutor", None)
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

        # Provider-native built-in tools. These executors never actually
        # run — the loader wraps them in a Tool carrying ``builtin_dict``
        # so the provider splices ``{"type": "web_search", ...}`` (etc.)
        # into its tools list. Local invocation raises NotImplementedError.
        elif tool_name == "web_search":
            executor_cls = getattr(mod, "WebSearchExecutor", None)
            if executor_cls:
                executor = executor_cls()
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "file_search":
            executor_cls = getattr(mod, "FileSearchExecutor", None)
            if executor_cls:
                executor = executor_cls()
                async def _impl(**kwargs: Any) -> str:
                    return await executor.execute(**kwargs)
                return _impl

        elif tool_name == "code_interpreter":
            executor_cls = getattr(mod, "CodeInterpreterExecutor", None)
            if executor_cls:
                executor = executor_cls()
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

        # Provider-native built-in tools carry a module-level
        # ``builtin_dict`` on their executor class — pick it up so the
        # Tool wraps the raw ``{"type": "web_search"}`` (etc.) shape that
        # Codex/OpenAI Responses splice into ``tools=[]``.
        builtin_dict = self._load_builtin_dict(tool_name)

        backgroundable = bool(schema_data.get("backgroundable", False))
        return Tool(
            name=name,
            description=description,
            func=impl,
            schema=input_schema,
            backgroundable=backgroundable,
            builtin_dict=builtin_dict,
        )

    def _load_builtin_dict(self, tool_name: str) -> dict | None:
        """Look up ``builtin_dict`` on the executor module, if any.

        The lookup is best-effort: built-in executors declare a class
        attribute called ``builtin_dict`` on their executor class (e.g.
        ``WebSearchExecutor.builtin_dict``). Regular toolhub modules don't
        define one and this returns ``None``.
        """
        mod = _load_executor_module(tool_name, self._toolhub_dir)
        if mod is None:
            return None
        # Prefer the known class-name pattern first to keep lookup cheap;
        # fall back to scanning module attrs for an Executor-named class
        # that happens to carry ``builtin_dict``.
        candidates: list[str] = [
            f"{''.join(p.title() for p in tool_name.split('_'))}Executor",
            "Executor",
        ]
        for cls_name in candidates:
            cls = getattr(mod, cls_name, None)
            if cls is not None:
                spec = getattr(cls, "builtin_dict", None)
                if spec:
                    return dict(spec)
        return None

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
