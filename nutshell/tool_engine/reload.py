"""reload_capabilities built-in tool factory.

Avoids circular imports by depending only on a Protocol, not on
the concrete Session class.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from nutshell.core.tool import Tool

if TYPE_CHECKING:
    pass


class _ReloadTarget(Protocol):
    _agent: object

    def _load_session_capabilities(self) -> None: ...


def create_reload_tool(session: _ReloadTarget) -> Tool:
    """Return a Tool that triggers capability hot-reload for *session*."""

    def _reload() -> str:
        session._load_session_capabilities()
        tool_names = [getattr(t, "name", "<unknown>") for t in getattr(session._agent, "tools", [])]
        skill_names = [getattr(s, "name", "<unknown>") for s in getattr(session._agent, "skills", [])]
        return (
            "Capabilities reloaded. "
            f"Tools ({len(tool_names)}): {_summarize_names(tool_names)}. "
            f"Skills ({len(skill_names)}): {_summarize_names(skill_names)}."
        )

    return Tool(
        name="reload_capabilities",
        description=(
            "Reload tools and skills from core/ without restarting the session. "
            "Call after creating or modifying a tool (.json + .sh) or skill (SKILL.md) "
            "to make the changes available in the current conversation. Returns a summary "
            "of the currently loaded tools and skills."
        ),
        func=_reload,
        schema={"type": "object", "properties": {}, "required": []},
    )


def _summarize_names(names: list[str], limit: int = 6) -> str:
    if not names:
        return "none"
    if len(names) <= limit:
        return ", ".join(names)
    shown = ", ".join(names[:limit])
    return f"{shown}, ..."
