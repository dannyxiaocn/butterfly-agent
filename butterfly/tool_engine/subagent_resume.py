"""subagent_resume — continue a conversation with an existing sub-agent.

Looks up the target child by ``display_name`` via the parent's panel entries,
posts a new user_input to the child's ``context.jsonl``, and blocks on
``BridgeSession.async_wait_for_reply`` until the matching turn lands or the
timeout fires. Mirrors ``SubAgentTool``'s cancel-cascade so an interrupt on
the parent flows through to the child.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from butterfly.tool_engine.sub_agent import (
    _DEFAULT_SESSIONS_BASE,
    _DEFAULT_SYSTEM_SESSIONS_BASE,
    _DEFAULT_TIMEOUT_SECONDS,
    _MIN_TIMEOUT_SECONDS,
    _find_sub_agent_by_name,
    _list_sub_agent_entries,
    _validate_name,
)


class SubAgentResumeTool:
    """Post a follow-up message to an existing sub-agent, await reply."""

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

    async def execute(self, **kwargs: Any) -> str:
        if not self._parent_session_id:
            return (
                "Error: subagent_resume tool was loaded without a parent session "
                "context. This indicates a misconfigured ToolLoader."
            )
        try:
            name_raw = kwargs["name"]
            message = kwargs["message"]
        except KeyError as exc:
            return f"Error: missing required arg {exc.args[0]!r}"
        try:
            name = _validate_name(name_raw)
        except ValueError as exc:
            return f"Error: {exc}"
        if not isinstance(message, str) or not message.strip():
            return "Error: message must be a non-empty string"
        timeout = max(
            float(kwargs.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS),
            float(_MIN_TIMEOUT_SECONDS),
        )

        entries = _list_sub_agent_entries(self._parent_session_id, self._sessions_base)
        target = _find_sub_agent_by_name(entries, name)
        if target is None:
            available = sorted(
                {(e.meta or {}).get("display_name", "") for e in entries}
                - {""}
            )
            hint = (
                f" Known names: {', '.join(repr(n) for n in available)}."
                if available
                else " No sub-agents have been spawned yet; call `subagent_new` first."
            )
            return f"Error: no sub-agent named {name!r} found.{hint}"

        child_id = (target.meta or {}).get("child_session_id")
        if not child_id:
            return (
                f"Error: sub-agent {name!r} is missing its child_session_id "
                f"(corrupt panel entry tid={target.tid}). Spawn a fresh one with "
                f"`subagent_new`."
            )

        # If the child session was stopped (e.g. parent called `butterfly stop`
        # on it earlier), un-stop it before posting — otherwise the user_input
        # sits idle on disk until someone manually resumes. Best-effort: we
        # still post even if start_session fails, so a timeout surfaces cleanly.
        self._ensure_child_active(child_id)

        from butterfly.runtime.bridge import BridgeSession
        bridge = BridgeSession(self._system_sessions_base / child_id)

        msg_id = bridge.send_message(message, caller="parent_agent", mode="interrupt")

        try:
            reply = await bridge.async_wait_for_reply(msg_id, timeout=timeout)
        except asyncio.CancelledError:
            # Mirror SubAgentTool: cascade the interrupt so the child drops its
            # in-flight work instead of churning on a reply the parent already
            # discarded. Best-effort; swallow failures so cancellation
            # propagation stays clean.
            try:
                bridge.send_interrupt()
            except Exception:
                pass
            raise

        if reply is None:
            return (
                f"[subagent_resume] timed out after {timeout:.0f}s waiting for "
                f"{name!r} (child_session_id={child_id}). The child is still "
                f"running — call `subagent_resume` again (optionally with a "
                f"larger timeout_seconds) to re-await its reply."
            )
        return reply

    def _ensure_child_active(self, child_id: str) -> None:
        """Flip a stopped child session back to active so it picks up the input.

        Silently no-ops when the session is already active, missing, or the
        service call raises. The subsequent ``async_wait_for_reply`` is the
        source of truth for whether the child actually responds.
        """
        try:
            from butterfly.service.sessions_service import (
                get_session,
                start_session,
            )
            info = get_session(child_id, self._sessions_base, self._system_sessions_base)
        except Exception:
            return
        if info is None:
            return
        if info.get("status") == "stopped":
            try:
                start_session(child_id, self._system_sessions_base)
            except Exception:
                pass
