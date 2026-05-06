"""Siri tool — natural-language dispatcher backed by a weak-model sub-agent.

The parent agent passes a single ``request`` string ("grep TODO under src/
and write the count to playground/todo_count.txt"). Siri spawns a fresh
child session running the ``tool_agent`` agent (whose ``config.yaml``
pins ``model: kimi-for-coding`` / ``provider: kimi-coding-plan``), waits
for its final reply, and returns it verbatim. The reply is contracted to
contain:

  1. a short summary of WHAT tool calls the child made, and
  2. the actual tool-call OUTPUT (or its key bits, when large).

Implementation: this is a thin facade around ``SubAgentTool`` that hard-
wires ``agent_name="tool_agent"`` and ``mode="executor"`` so the schema
the parent sees is just ``request`` (plus optional ``name`` /
``timeout_seconds``). Reusing ``SubAgentTool`` means we inherit the
existing depth cap, panel display, cancellation cascade, and timeout
semantics for free — siri is just a different LLM-visible surface on top
of the same machinery.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from butterfly.session_engine.panel import PanelEntry
from butterfly.tool_engine.background import BackgroundContext
from butterfly.tool_engine.sub_agent import (
    SubAgentRunner,
    SubAgentTool,
    _validate_name,
)


_AGENT_NAME = "tool_agent"
_MODE = "executor"
_NAME_FALLBACK_LEN = 40


def _default_name(request: str) -> str:
    return " ".join(request.split())[:_NAME_FALLBACK_LEN].strip()


def _compose_task(request: str) -> str:
    return (
        "You are tool_agent, invoked through the `siri` tool. The parent "
        "agent has handed you a natural-language request. Pick the right "
        "tool calls, execute them, and reply with:\n\n"
        "  1. A one-line summary of WHAT tools you called.\n"
        "  2. The KEY OUTPUT from those tool calls (paste the relevant "
        "stdout / file contents / search hits — trim aggressively if huge).\n\n"
        "End your reply with [DONE], [BLOCKED], or [ERROR].\n\n"
        f"## Request\n\n{request}"
    )


def _validate_request(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("siri: 'request' must be a string")
    trimmed = raw.strip()
    if not trimmed:
        raise ValueError("siri: 'request' must be a non-empty string")
    return trimmed


# ── Sync tool ────────────────────────────────────────────────────────────────


class SiriExecutor:
    """Synchronous siri executor.

    Constructed by ``ToolLoader`` with the parent session's id + base
    paths so the child session lands in the right ``sessions/`` /
    ``_sessions/`` trees and the panel card is attributed to the parent.
    """

    def __init__(
        self,
        parent_session_id: str | None = None,
        sessions_base: Path | None = None,
        system_sessions_base: Path | None = None,
        agent_base: Path | None = None,
    ) -> None:
        self._sub = SubAgentTool(
            parent_session_id=parent_session_id,
            sessions_base=sessions_base,
            system_sessions_base=system_sessions_base,
            agent_base=agent_base,
        )

    async def execute(self, **kwargs: Any) -> str:
        try:
            request = _validate_request(kwargs.get("request"))
        except ValueError as exc:
            return f"Error: {exc}"
        name_raw = kwargs.get("name") or _default_name(request)
        try:
            name = _validate_name(name_raw)
        except ValueError as exc:
            return f"Error: {exc}"
        sub_kwargs: dict[str, Any] = {
            "task": _compose_task(request),
            "mode": _MODE,
            "name": name,
            "agent_name": _AGENT_NAME,
        }
        if kwargs.get("timeout_seconds") is not None:
            sub_kwargs["timeout_seconds"] = kwargs["timeout_seconds"]
        return await self._sub.execute(**sub_kwargs)


# ── Background runner ───────────────────────────────────────────────────────


class SiriRunner:
    """``BackgroundRunner`` for siri — delegates to ``SubAgentRunner``.

    Backgrounded ``siri`` calls reuse the same panel + events plumbing as
    backgrounded sub_agent / workflow. We just rewrite the input dict
    into the sub_agent shape (``task`` + ``mode`` + ``name`` +
    ``agent_name``) before handing off.
    """

    def __init__(
        self,
        parent_session_id: str,
        sessions_base: Path,
        system_sessions_base: Path,
        agent_base: Path,
    ) -> None:
        self._inner = SubAgentRunner(
            parent_session_id=parent_session_id,
            sessions_base=sessions_base,
            system_sessions_base=system_sessions_base,
            agent_base=agent_base,
        )

    @staticmethod
    def _rewrite(input: dict[str, Any]) -> dict[str, Any]:
        request = _validate_request(input.get("request"))
        name = _validate_name(input.get("name") or _default_name(request))
        out: dict[str, Any] = {
            "task": _compose_task(request),
            "mode": _MODE,
            "name": name,
            "agent_name": _AGENT_NAME,
        }
        if input.get("timeout_seconds") is not None:
            out["timeout_seconds"] = input["timeout_seconds"]
        return out

    def validate(self, input: dict[str, Any]) -> None:
        # Mirror SubAgentRunner.validate's submit-time strictness: check
        # ``request`` shape AND any caller-supplied ``name``. The runner-side
        # rewrite would catch a bad name eventually, but only at run-time —
        # validating here surfaces the failure when the task is queued.
        _validate_request(input.get("request"))
        if input.get("name") is not None:
            _validate_name(input["name"])

    async def run(
        self,
        ctx: BackgroundContext,
        tid: str,
        entry: PanelEntry,
        input: dict[str, Any],
        polling_interval: int | None,
    ) -> int | None:
        return await self._inner.run(
            ctx, tid, entry, self._rewrite(input), polling_interval,
        )

    async def kill(self, ctx: BackgroundContext, tid: str) -> bool:
        return await self._inner.kill(ctx, tid)
