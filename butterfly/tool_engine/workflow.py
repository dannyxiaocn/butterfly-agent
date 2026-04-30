"""Workflow executor + background runner — chained sub-agent pipeline.

A workflow is just an ordered list of sub-agent invocations. Each step
reuses the existing ``SubAgentTool`` / ``SubAgentRunner`` plumbing so the
parent session sees one panel card per step (same shape as a direct
``subagent_new`` call). The output of step N is spliced into step N+1's
prompt via the literal ``{prev}`` placeholder.

This file contains:

  * ``WorkflowExecutor`` — synchronous runner used when the agent invokes
    ``workflow`` with ``run_in_background=false``.

  * ``WorkflowRunner`` — backgroundable runner registered with the
    session's ``BackgroundTaskManager``. Mirrors ``SubAgentRunner``'s
    contract so backgrounded workflows flow through the same
    panel + events plumbing.

The two intentionally share zero subclassing — one is sync (returns a
string), the other yields a panel entry's worth of state. Both delegate
their per-step work to ``SubAgentTool.execute`` so we don't duplicate the
init_session / wait_for_reply code path.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from butterfly.tool_engine.background import BackgroundContext, BackgroundEvent
from butterfly.tool_engine.sub_agent import SubAgentTool


_PREV_TOKEN = "{prev}"
_VALID_STEP_MODES = ("explorer", "executor")


def _validate_steps(steps: Any) -> list[dict]:
    if not isinstance(steps, list) or not steps:
        raise ValueError("workflow: 'steps' must be a non-empty list")
    out: list[dict] = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"workflow: steps[{i}] must be a mapping")
        name = str(step.get("name") or "").strip()
        task = str(step.get("task") or "").strip()
        mode = str(step.get("mode") or "executor").strip()
        agent_name = step.get("agent")
        if not name:
            raise ValueError(f"workflow: steps[{i}].name is required")
        if not task:
            raise ValueError(f"workflow: steps[{i}].task is required")
        if mode not in _VALID_STEP_MODES:
            raise ValueError(
                f"workflow: steps[{i}].mode must be one of {_VALID_STEP_MODES}, got {mode!r}"
            )
        if agent_name is not None and not isinstance(agent_name, str):
            raise ValueError(f"workflow: steps[{i}].agent must be a string when set")
        out.append({"name": name, "task": task, "mode": mode, "agent": agent_name})
    return out


def _format_step_header(idx: int, step: dict) -> str:
    parts = [f"step {idx}", step["name"]]
    if step["agent"]:
        parts.append(f"agent={step['agent']}")
    parts.append(f"mode={step['mode']}")
    return f"[workflow {' · '.join(parts)}]"


def _format_full_log(rendered: list[tuple[dict, str]]) -> str:
    """Render the per-step reply log returned to the caller as the tool result."""
    chunks = []
    for i, (step, reply) in enumerate(rendered, 1):
        chunks.append(f"{_format_step_header(i, step)}\n{reply}")
    return "\n\n---\n\n".join(chunks)


# ── Sync executor ───────────────────────────────────────────────────────────


class WorkflowExecutor:
    """Run a workflow in the parent's turn (synchronous). Returns the log."""

    def __init__(
        self,
        *,
        parent_session_id: str | None,
        sessions_base: Path,
        system_sessions_base: Path,
        agent_base: Path,
    ) -> None:
        self._sub = SubAgentTool(
            parent_session_id=parent_session_id,
            sessions_base=sessions_base,
            system_sessions_base=system_sessions_base,
            agent_base=agent_base,
        )

    async def execute(self, **kwargs: Any) -> str:
        try:
            steps = _validate_steps(kwargs.get("steps"))
        except ValueError as exc:
            return f"Error: {exc}"
        prev = ""
        rendered: list[tuple[dict, str]] = []
        for step in steps:
            task = step["task"].replace(_PREV_TOKEN, prev)
            sub_kwargs = {
                "name": step["name"],
                "task": task,
                "mode": step["mode"],
            }
            if step["agent"]:
                sub_kwargs["agent_name"] = step["agent"]
            reply = await self._sub.execute(**sub_kwargs)
            rendered.append((step, reply))
            # The sub-agent tool's result IS the child's final reply; if
            # the child timed out it's a notice string instead. Either way
            # we feed it forward so the next step can react (e.g. detect
            # [BLOCKED] / [ERROR] prefixes and adjust).
            prev = reply
        return _format_full_log(rendered)


# ── Background runner ────────────────────────────────────────────────────────


class WorkflowRunner:
    """``BackgroundRunner`` for ``workflow`` — same wrapping pattern as
    SubAgentRunner so backgrounded workflows flow through the panel +
    events plumbing without bespoke code."""

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
        _validate_steps(input.get("steps"))

    async def run(
        self,
        ctx: BackgroundContext,
        tid: str,
        entry,
        input: dict[str, Any],
        polling_interval: int | None,
    ) -> int | None:
        try:
            steps = _validate_steps(input.get("steps"))
        except ValueError as exc:
            entry.meta = {**(entry.meta or {}), "error": str(exc)}
            ctx.save_entry(entry)
            return -1

        sub = SubAgentTool(
            parent_session_id=self._parent_session_id,
            sessions_base=self._sessions_base,
            system_sessions_base=self._system_sessions_base,
            agent_base=self._agent_base,
        )

        prev = ""
        rendered: list[tuple[dict, str]] = []
        killed = False
        for i, step in enumerate(steps, 1):
            # Honour kill-between-steps: re-load the panel entry from disk
            # so we observe a status flip set by ``kill()`` (or by another
            # process touching the panel file). Without this check the
            # runner plows through every step regardless of status —
            # confirmed in PR #58 review by the test_kill_aborts_remaining_steps
            # repro.
            disk = ctx.load_entry(tid)
            if disk is not None and disk.is_terminal():
                killed = True
                break
            entry.meta = {
                **(entry.meta or {}),
                "current_step": i,
                "step_name": step["name"],
                "step_count": len(steps),
            }
            entry.last_activity_at = time.time()
            ctx.save_entry(entry)
            ctx.emit(BackgroundEvent(
                tid=tid,
                kind="progress",
                entry=entry,
                delta_text=_format_step_header(i, step),
            ))
            task = step["task"].replace(_PREV_TOKEN, prev)
            sub_kwargs = {
                "name": step["name"],
                "task": task,
                "mode": step["mode"],
            }
            if step["agent"]:
                sub_kwargs["agent_name"] = step["agent"]
            reply = await sub.execute(**sub_kwargs)
            rendered.append((step, reply))
            prev = reply

        cur = ctx.load_entry(tid) or entry
        cur.meta = {
            **(cur.meta or {}),
            "result": _format_full_log(rendered),
            "result_text": prev,
            "step_count": len(steps),
            "killed": killed,
        }
        ctx.save_entry(cur)
        # Return None on kill so the BackgroundTaskManager's terminal
        # event preserves the kill marker the panel entry already
        # carries; 0 is "success" and would otherwise overwrite it.
        return None if killed else 0

    async def kill(self, ctx: BackgroundContext, tid: str) -> bool:
        # v1: workflow itself doesn't track child PIDs — each step's child
        # session is killable through the standard sub-agent kill path. A
        # bare ``workflow`` kill marks the panel entry stopped so the loop
        # exits between steps; in-flight step waits for the current
        # ``SubAgentTool.execute`` to return naturally (or its own
        # cascade-cancel) before the manager fires the terminal event.
        from butterfly.session_engine.panel import STATUS_KILLED
        entry = ctx.load_entry(tid)
        if entry is None or entry.is_terminal():
            return False
        entry.status = STATUS_KILLED
        entry.finished_at = time.time()
        ctx.save_entry(entry)
        return True
