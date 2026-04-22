"""subagent_list — enumerate the parent session's sub-agent children.

Reads the parent's ``core/panel/`` dir for ``type=sub_agent`` entries (the
canonical record written by ``SubAgentRunner`` and ``BackgroundTaskManager``)
and formats a compact listing the LLM can act on — the ``name`` column is the
handle ``subagent_resume`` takes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from butterfly.session_engine.panel import TERMINAL_STATUSES
from butterfly.tool_engine.sub_agent import (
    _DEFAULT_SESSIONS_BASE,
    _DEFAULT_SYSTEM_SESSIONS_BASE,
    _list_sub_agent_entries,
)


class SubAgentListTool:
    """List sub-agents spawned by this parent session."""

    def __init__(
        self,
        parent_session_id: str | None = None,
        sessions_base: Path | None = None,
        system_sessions_base: Path | None = None,
    ) -> None:
        self._parent_session_id = parent_session_id
        self._sessions_base = Path(sessions_base) if sessions_base else _DEFAULT_SESSIONS_BASE
        self._system_sessions_base = (
            Path(system_sessions_base) if system_sessions_base else _DEFAULT_SYSTEM_SESSIONS_BASE
        )

    async def execute(self, **kwargs: Any) -> str:  # noqa: ARG002 — schema has no inputs
        if not self._parent_session_id:
            return (
                "Error: subagent_list tool was loaded without a parent session "
                "context. This indicates a misconfigured ToolLoader."
            )
        entries = _list_sub_agent_entries(self._parent_session_id, self._sessions_base)
        if not entries:
            return (
                "No sub-agents yet. Call `subagent_new` to spawn one; its "
                "`name` will then be listed here and usable by `subagent_resume`."
            )

        running = sum(1 for e in entries if not e.is_terminal())
        header = (
            f"{len(entries)} sub-agent(s) — {running} running, "
            f"{len(entries) - running} finished:"
        )
        lines = [header]
        for e in entries:
            meta = e.meta or {}
            name = meta.get("display_name", "(unnamed)")
            mode = meta.get("mode", "?")
            child_id = meta.get("child_session_id", "?")
            status = _friendly_status(e.status)
            lines.append(
                f"  - name={name!r}  mode={mode}  status={status}  child_session_id={child_id}"
            )
        lines.append("")
        lines.append(
            "Resume one with: subagent_resume(name=<name above>, message=<follow-up>)."
        )
        return "\n".join(lines)


def _friendly_status(status: str) -> str:
    if status in TERMINAL_STATUSES:
        return status
    # "running" and "stalled" are both live panel states; surface as-is so the
    # LLM can judge whether to resume now or later.
    return status
