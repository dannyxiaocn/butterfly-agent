"""Sub-agent tool — spawn a child session, return its final reply.

This module defines two cooperating classes:

* ``SubAgentTool`` — synchronous executor (returned by the ToolLoader when
  the parent agent calls ``sub_agent`` with ``run_in_background=false``).
  Blocks the parent's turn until the child produces a reply or the timeout
  expires.

* ``SubAgentRunner`` — background runner registered with each session's
  ``BackgroundTaskManager`` so the same call with ``run_in_background=true``
  flows through the existing panel + events plumbing as any other
  backgroundable tool. Only the *runner half* is shared with the bash flow;
  no new lifecycle infrastructure is introduced.

Sub-agent semantics (also stated in the tool description and mode prompts):
the parent only ever sees the child's **final reply**. Intermediate tool
calls, thinking, and partial messages are NOT forwarded to the parent's
context. Inspecting child progress is done via the panel (which mirrors the
last activity) or by opening the child session in the sidebar.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from butterfly.session_engine.panel import PanelEntry, STATUS_KILLED
from butterfly.session_engine.session_init import init_session
from butterfly.tool_engine.background import BackgroundContext, BackgroundEvent

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_SESSIONS_BASE = _REPO_ROOT / "sessions"
_DEFAULT_SYSTEM_SESSIONS_BASE = _REPO_ROOT / "_sessions"
_DEFAULT_AGENT_BASE = _REPO_ROOT / "agenthub"

_VALID_MODES = ("explorer", "executor")
_DEFAULT_TIMEOUT_SECONDS = 600
# Caller can opt into a longer wait but we never go below 30s — anything
# shorter is a configuration error (init_session itself takes ~1-2s for
# venv creation on a cold session).
_MIN_TIMEOUT_SECONDS = 30
# Nesting cap. 0 = top-level user session; 1 = its sub-agent; 2 = grand-
# child. We refuse a spawn attempt at MAX so the deepest a tree gets is
# user → parent sub → grandchild sub. Protects against a runaway chain
# where each executor-mode child calls sub_agent again. Cap lives in
# each child's manifest so the limit survives daemon restarts. (PR #28
# review nit.)
_MAX_SUB_AGENT_DEPTH = 2


def _new_child_id() -> str:
    """Same id format Session uses internally."""
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "-" + uuid.uuid4().hex[:4]


def _compose_initial_message(task: str, mode: str) -> str:
    return (
        f"[Sub-agent task — mode: {mode}]\n\n"
        f"You are a sub-agent spawned by a parent session. Your full mode "
        f"contract lives in your system prompt (mode.md). The parent only "
        f"sees your final reply — intermediate steps stay local. End your "
        f"reply with [DONE], [REVIEW], [BLOCKED], or [ERROR].\n\n"
        f"## Task\n\n{task}"
    )


def _read_parent_manifest(parent_session_id: str, sys_base: Path) -> dict:
    manifest_path = sys_base / parent_session_id / "manifest.json"
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_parent_agent(parent_session_id: str, sys_base: Path) -> str:
    return _read_parent_manifest(parent_session_id, sys_base).get("agent", "")


def _parent_sub_agent_depth(parent_session_id: str, sys_base: Path) -> int:
    """Depth of the parent in the sub-agent tree.

    Top-level user sessions have no ``sub_agent_depth`` recorded and read as 0;
    each spawn increments it by 1 and records it on the child's manifest.
    """
    raw = _read_parent_manifest(parent_session_id, sys_base).get("sub_agent_depth", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _validate_mode(mode: Any) -> None:
    if mode not in _VALID_MODES:
        raise ValueError(
            f"sub_agent: mode must be one of {_VALID_MODES}, got {mode!r}"
        )


def _spawn_child(
    *,
    parent_session_id: str,
    mode: str,
    task: str,
    sessions_base: Path,
    system_sessions_base: Path,
    agent_base: Path,
) -> tuple[str, str, str]:
    """Create the child session on disk. Returns ``(child_id, msg_id, agent_name)``.

    Raises ``RuntimeError`` when the parent has already reached
    ``_MAX_SUB_AGENT_DEPTH`` — otherwise a runaway executor chain could
    fork sessions unbounded.
    """
    parent_manifest = _read_parent_manifest(parent_session_id, system_sessions_base)
    agent_name = parent_manifest.get("agent", "")
    if not agent_name:
        raise RuntimeError(
            f"sub_agent: cannot read parent agent from {parent_session_id}/manifest.json"
        )
    try:
        parent_depth = int(parent_manifest.get("sub_agent_depth", 0))
    except (TypeError, ValueError):
        parent_depth = 0
    if parent_depth >= _MAX_SUB_AGENT_DEPTH:
        raise RuntimeError(
            f"sub_agent: spawn refused — parent is already at depth "
            f"{parent_depth} (max {_MAX_SUB_AGENT_DEPTH}). Recursive "
            "sub-agent chains are bounded to prevent runaway forks."
        )
    child_id = _new_child_id()
    msg_id = str(uuid.uuid4())
    init_session(
        session_id=child_id,
        agent_name=agent_name,
        sessions_base=sessions_base,
        system_sessions_base=system_sessions_base,
        agent_base=agent_base,
        initial_message=_compose_initial_message(task, mode),
        initial_message_id=msg_id,
        parent_session_id=parent_session_id,
        mode=mode,
        sub_agent_depth=parent_depth + 1,
    )
    return child_id, msg_id, agent_name


async def _wait_for_reply(
    *, child_id: str, msg_id: str, system_sessions_base: Path, timeout: float
) -> str | None:
    """Block until the child emits the matching turn or the timeout fires.

    Returns the child's final assistant text, or None on timeout.
    """
    # Local import: BridgeSession imports FileIPC which has a heavier import
    # surface — keep it off the toolhub's hot path.
    from butterfly.runtime.bridge import BridgeSession

    bridge = BridgeSession(system_sessions_base / child_id)
    return await bridge.async_wait_for_reply(msg_id, timeout=timeout)


def _format_result(child_id: str, mode: str, reply: str | None, timeout: float) -> str:
    if reply is None:
        return (
            f"[sub_agent] timed out after {timeout:.0f}s waiting for child "
            f"{child_id}. The child session is still running — open it in "
            f"the sidebar to inspect progress."
        )
    return f"[sub_agent · child={child_id} · mode={mode}]\n{reply}"


# ── Sync tool ────────────────────────────────────────────────────────────────


class SubAgentTool:
    """Synchronous sub_agent executor.

    Constructed by ToolLoader with the parent session's id + base paths so
    the child shows up in the right ``sessions/`` / ``_sessions/`` trees.
    """

    def __init__(
        self,
        parent_session_id: str | None = None,
        sessions_base: Path | None = None,
        system_sessions_base: Path | None = None,
        agent_base: Path | None = None,
    ) -> None:
        self._parent_session_id = parent_session_id
        self._sessions_base = Path(sessions_base) if sessions_base else _DEFAULT_SESSIONS_BASE
        self._system_sessions_base = (
            Path(system_sessions_base) if system_sessions_base else _DEFAULT_SYSTEM_SESSIONS_BASE
        )
        self._agent_base = Path(agent_base) if agent_base else _DEFAULT_AGENT_BASE

    async def execute(self, **kwargs: Any) -> str:
        if not self._parent_session_id:
            return (
                "Error: sub_agent tool was loaded without a parent session "
                "context. This indicates a misconfigured ToolLoader."
            )
        try:
            task = kwargs["task"]
            mode = kwargs["mode"]
        except KeyError as exc:
            return f"Error: missing required arg {exc.args[0]!r}"
        try:
            _validate_mode(mode)
        except ValueError as exc:
            return f"Error: {exc}"
        timeout = max(
            float(kwargs.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS),
            float(_MIN_TIMEOUT_SECONDS),
        )
        try:
            child_id, msg_id, _agent = _spawn_child(
                parent_session_id=self._parent_session_id,
                mode=mode,
                task=task,
                sessions_base=self._sessions_base,
                system_sessions_base=self._system_sessions_base,
                agent_base=self._agent_base,
            )
        except Exception as exc:  # noqa: BLE001 — surface spawn failures cleanly
            return f"Error: sub_agent spawn failed: {exc}"
        reply = await _wait_for_reply(
            child_id=child_id,
            msg_id=msg_id,
            system_sessions_base=self._system_sessions_base,
            timeout=timeout,
        )
        return _format_result(child_id, mode, reply, timeout)


# ── Background runner ───────────────────────────────────────────────────────


class SubAgentRunner:
    """``BackgroundRunner`` impl for sub_agent.

    Registered with the per-session ``BackgroundTaskManager`` so that
    ``sub_agent`` calls with ``run_in_background=true`` route through the
    same panel + events pipeline as bash background tasks.
    """

    # Polled even if no polling_interval was given so the panel entry stays
    # fresh — sub-agent progress is "what tool is the child running now",
    # which the parent's UI uses to render the working state.
    _DEFAULT_PROGRESS_INTERVAL = 5

    def __init__(
        self,
        parent_session_id: str,
        sessions_base: Path,
        system_sessions_base: Path,
        agent_base: Path,
    ) -> None:
        self._parent_session_id = parent_session_id
        self._sessions_base = Path(sessions_base)
        self._system_sessions_base = Path(system_sessions_base)
        self._agent_base = Path(agent_base)

    def validate(self, input: dict[str, Any]) -> None:
        if not input.get("task"):
            raise ValueError("sub_agent: input.task is required")
        _validate_mode(input.get("mode"))

    async def run(
        self,
        ctx: BackgroundContext,
        tid: str,
        entry: PanelEntry,
        input: dict[str, Any],
        polling_interval: int | None,
    ) -> int | None:
        task = input["task"]
        mode = input["mode"]
        timeout = max(
            float(input.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS),
            float(_MIN_TIMEOUT_SECONDS),
        )
        try:
            child_id, msg_id, agent_name = _spawn_child(
                parent_session_id=self._parent_session_id,
                mode=mode,
                task=task,
                sessions_base=self._sessions_base,
                system_sessions_base=self._system_sessions_base,
                agent_base=self._agent_base,
            )
        except Exception as exc:  # noqa: BLE001
            entry.meta = {**(entry.meta or {}), "error": str(exc)}
            ctx.save_entry(entry)
            return -1

        entry.meta = {
            **(entry.meta or {}),
            "child_session_id": child_id,
            "mode": mode,
            "agent": agent_name,
            "timeout_seconds": timeout,
        }
        ctx.save_entry(entry)

        progress_task: asyncio.Task | None = None
        interval = polling_interval if polling_interval and polling_interval > 0 else self._DEFAULT_PROGRESS_INTERVAL
        progress_task = asyncio.create_task(
            self._progress_loop(ctx, tid, child_id, interval),
            name=f"sub_agent_progress_{tid}",
        )

        try:
            reply = await _wait_for_reply(
                child_id=child_id,
                msg_id=msg_id,
                system_sessions_base=self._system_sessions_base,
                timeout=timeout,
            )
        finally:
            if progress_task is not None and not progress_task.done():
                progress_task.cancel()
                try:
                    await progress_task
                except (asyncio.CancelledError, Exception):
                    pass

        cur = ctx.load_entry(tid) or entry
        cur.meta = {
            **(cur.meta or {}),
            "result": _format_result(child_id, mode, reply, timeout),
            "result_text": reply or "",
            "timed_out": reply is None,
        }
        ctx.save_entry(cur)
        return 0 if reply is not None else 1

    async def kill(self, ctx: BackgroundContext, tid: str) -> bool:
        entry = ctx.load_entry(tid)
        if entry is None or entry.is_terminal():
            return False
        # Cooperative stop: mark child stopped so its daemon stays paused.
        # The runner's wait loop will eventually time out; the manager picks
        # up STATUS_KILLED from the entry and emits the right event kind.
        child_id = (entry.meta or {}).get("child_session_id")
        if child_id:
            try:
                from butterfly.service.sessions_service import stop_session
                stop_session(child_id, self._system_sessions_base)
            except Exception:
                pass
        entry.status = STATUS_KILLED
        entry.finished_at = time.time()
        ctx.save_entry(entry)
        return True

    async def _progress_loop(
        self,
        ctx: BackgroundContext,
        tid: str,
        child_id: str,
        interval: int,
    ) -> None:
        """Tail the child's events.jsonl and refresh the panel entry's
        ``last_child_state`` summary on every tick."""
        from butterfly.runtime.ipc import FileIPC

        ipc = FileIPC(self._system_sessions_base / child_id)
        last_summary = ""
        try:
            while True:
                await asyncio.sleep(interval)
                summary = self._summarize_child_state(ipc)
                if not summary or summary == last_summary:
                    continue
                entry = ctx.load_entry(tid)
                if entry is None:
                    continue
                entry.meta = {**(entry.meta or {}), "last_child_state": summary}
                entry.last_activity_at = time.time()
                ctx.save_entry(entry)
                ctx.emit(BackgroundEvent(
                    tid=tid, kind="progress", entry=entry, delta_text=summary,
                ))
                last_summary = summary
        except asyncio.CancelledError:
            return

    @staticmethod
    def _summarize_child_state(ipc) -> str:
        """Read the last few entries of the child's events.jsonl and produce
        a one-line summary (e.g. ``running tool: bash``).

        The field names below MUST match what the child's own ``Session``
        actually writes:
          - ``_make_tool_call_callback`` emits ``{"type":"tool_call","name":...}``
          - ``_set_model_status`` emits ``{"type":"model_status","state":...}``
        Reported in PR #28 review as Bug #2.
        """
        evt_path = getattr(ipc, "events_path", None)
        if evt_path is None or not Path(evt_path).exists():
            return ""
        # Cheap tail: read the file fully — events.jsonl is bounded by the
        # session's lifetime and the panel entry refreshes every interval, so
        # this isn't a hot path.
        try:
            with Path(evt_path).open("r", encoding="utf-8") as f:
                lines = f.readlines()[-12:]  # window over the last few events
        except OSError:
            return ""
        last_tool = ""
        last_status = ""
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type", "")
            if etype == "tool_call" and not last_tool:
                last_tool = evt.get("name", "")
            elif etype == "model_status" and not last_status:
                last_status = evt.get("state", "")
            if last_tool and last_status:
                break
        if last_tool:
            return f"running tool: {last_tool}"
        if last_status:
            return f"model: {last_status}"
        return ""
