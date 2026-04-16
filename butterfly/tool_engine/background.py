"""BackgroundTaskManager — owns the lifecycle of non-blocking tool calls.

One manager per session. Tool calls with `run_in_background=true` are routed
here by the agent loop; the manager:

  1. Looks up a `BackgroundRunner` registered for the tool's name.
  2. Creates a `PanelEntry` under `sessions/<id>/core/panel/<tid>.json`.
  3. Hands off to the runner, which executes the actual work and may emit
     `progress` / `stalled` events along the way.
  4. After the runner returns, fires a single `completed` (or `killed`)
     event on `asyncio.Queue` for the session daemon to drain.

Runners encapsulate "what does this backgroundable tool actually do" — for
`bash`, that's "spawn a subprocess and stream stdout"; for `sub_agent`, it's
"spawn a child session and wait for the first reply". The manager itself is
runner-agnostic: it owns the panel + event + lifecycle plumbing only.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from butterfly.session_engine.panel import (
    PanelEntry,
    STATUS_COMPLETED,
    STATUS_KILLED,
    STATUS_KILLED_BY_RESTART,
    STATUS_RUNNING,
    STATUS_STALLED,
    TYPE_PENDING_TOOL,
    create_pending_tool_entry,
    list_entries,
    load_entry,
    save_entry,
    sweep_killed_by_restart,
)

# 5 minutes of no output → fire stall once
_STALL_SECONDS = 300.0

# Read chunk size for draining stdout
_CHUNK_SIZE = 4096


@dataclass
class BackgroundEvent:
    """An event emitted by the manager for the session daemon to handle."""
    tid: str
    kind: str          # "completed" | "stalled" | "progress" | "killed_by_restart"
    entry: PanelEntry  # Current state snapshot at event time
    delta_text: str = ""  # For "progress" events: new bytes since last delivery


@dataclass
class BackgroundContext:
    """Facilities a runner uses to interact with the panel + event queue.

    The manager constructs one of these per session and passes it to every
    runner call. Runners must not hold references across sessions.
    """
    panel_dir: Path
    tool_results_dir: Path
    venv_env_provider: Callable[[], dict[str, str] | None] | None
    _emit: Callable[[BackgroundEvent], None]
    # Optional path boundary for sub-agent's explorer mode. Runners that
    # spawn shells (BashRunner, future SessionShellRunner) MUST pin cwd to
    # ``guardian.root`` and export ``BUTTERFLY_GUARDIAN_ROOT`` when set.
    # PR #28 review Bug #5: without this, ``run_in_background=true`` on
    # ``bash`` would let an explorer-mode child write outside its sandbox.
    guardian: "object | None" = None

    def emit(self, evt: BackgroundEvent) -> None:
        self._emit(evt)

    def load_entry(self, tid: str) -> PanelEntry | None:
        return load_entry(self.panel_dir, tid)

    def save_entry(self, entry: PanelEntry) -> None:
        save_entry(self.panel_dir, entry)


class BackgroundRunner(Protocol):
    """The contract every backgroundable tool implements.

    Runners are registered with `BackgroundTaskManager.register_runner(name, runner)`
    and may be plain objects (no inheritance required) so long as they expose
    these three methods.
    """

    def validate(self, input: dict[str, Any]) -> None:
        """Raise ValueError synchronously if `input` cannot be run.

        Called by `spawn()` BEFORE the panel entry is created so callers see
        configuration errors immediately rather than as silent panel rows.
        """
        ...

    async def run(
        self,
        ctx: BackgroundContext,
        tid: str,
        entry: PanelEntry,
        input: dict[str, Any],
        polling_interval: int | None,
    ) -> int | None:
        """Execute the work to completion. Return an exit_code (or None).

        The manager fires the terminal `completed` event after this returns.
        Runners SHOULD update `entry.status` to `STATUS_KILLED` on graceful
        cancellation so the manager preserves the kill marker.
        """
        ...

    async def kill(self, ctx: BackgroundContext, tid: str) -> bool:
        """Best-effort stop of in-flight work for `tid`. Return True if anything
        was actually stopped (used by callers to distinguish 'killed something'
        vs 'already done')."""
        ...


# ── BashRunner — the original (and default) backgroundable runner ────────────


class BashRunner:
    """The original BackgroundTaskManager logic, repackaged as a runner.

    Spawns `input["command"]` via the shell, streams merged stdout/stderr to
    `tool_results_dir/<tid>.txt`, and runs a stall watchdog plus optional
    polling-interval progress ticks.
    """

    def validate(self, input: dict[str, Any]) -> None:
        if not input.get("command"):
            raise ValueError("BashRunner: input.command is required")

    async def run(
        self,
        ctx: BackgroundContext,
        tid: str,
        entry: PanelEntry,
        input: dict[str, Any],
        polling_interval: int | None,
    ) -> int | None:
        command = input["command"]
        workdir = input.get("workdir")
        stdin = input.get("stdin")
        env = ctx.venv_env_provider() if ctx.venv_env_provider else None

        # Guardian (sub-agent explorer mode) overrides cwd to the boundary
        # root and exports BUTTERFLY_GUARDIAN_ROOT — same contract the
        # inline BashExecutor enforces. Without this, run_in_background=true
        # would let an explorer-mode child execute bash with cwd anywhere
        # (PR #28 review Bug #5).
        if ctx.guardian is not None:
            guardian_root = str(ctx.guardian.root)
            workdir = guardian_root
            if env is None:
                env = os.environ.copy()
            env = dict(env)  # don't mutate venv_env_provider's return
            env["BUTTERFLY_GUARDIAN_ROOT"] = guardian_root

        output_file = ctx.tool_results_dir / f"{tid}.txt"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        entry.output_file = str(output_file)
        ctx.save_entry(entry)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=workdir,
                env=env,
                start_new_session=True,
            )
        except Exception as exc:
            output_file.write_text(f"[spawn failed: {exc}]", encoding="utf-8")
            return -1

        cur = ctx.load_entry(tid)
        if cur is not None:
            cur.pid = proc.pid
            ctx.save_entry(cur)

        if stdin is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin.encode())
                await proc.stdin.drain()
            finally:
                proc.stdin.close()

        drain_task = asyncio.create_task(
            self._drain_stdout(ctx, proc, output_file, tid),
            name=f"bgtask_{tid}_drain",
        )
        control_task = asyncio.create_task(
            self._control_loop(ctx, proc, tid, polling_interval),
            name=f"bgtask_{tid}_control",
        )
        try:
            await proc.wait()
        finally:
            try:
                await asyncio.wait_for(drain_task, timeout=2.0)
            except asyncio.TimeoutError:
                drain_task.cancel()
            control_task.cancel()
            try:
                await control_task
            except (asyncio.CancelledError, Exception):
                pass

        return proc.returncode

    async def kill(self, ctx: BackgroundContext, tid: str) -> bool:
        entry = ctx.load_entry(tid)
        if entry is None or entry.is_terminal():
            return False
        if entry.pid:
            try:
                os.killpg(os.getpgid(entry.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        entry.status = STATUS_KILLED
        entry.finished_at = time.time()
        ctx.save_entry(entry)
        return True

    async def _drain_stdout(
        self,
        ctx: BackgroundContext,
        proc: asyncio.subprocess.Process,
        output_file: Path,
        tid: str,
    ) -> None:
        assert proc.stdout is not None
        with output_file.open("ab") as out:
            while True:
                chunk = await proc.stdout.read(_CHUNK_SIZE)
                if not chunk:
                    return
                out.write(chunk)
                out.flush()
                entry = ctx.load_entry(tid)
                if entry is None:
                    continue
                entry.last_activity_at = time.time()
                try:
                    entry.output_bytes = output_file.stat().st_size
                except OSError:
                    pass
                ctx.save_entry(entry)

    async def _control_loop(
        self,
        ctx: BackgroundContext,
        proc: asyncio.subprocess.Process,
        tid: str,
        polling_interval: int | None,
    ) -> None:
        stall_fired = False
        last_progress_tick = time.time()
        try:
            while proc.returncode is None:
                await asyncio.sleep(1.0)
                now = time.time()
                entry = ctx.load_entry(tid)
                if entry is None:
                    continue
                silent = now - (entry.last_activity_at or entry.started_at or now)

                if not stall_fired and silent >= _STALL_SECONDS:
                    entry.status = STATUS_STALLED
                    ctx.save_entry(entry)
                    ctx.emit(BackgroundEvent(
                        tid=tid, kind="stalled", entry=entry,
                    ))
                    stall_fired = True

                if polling_interval and (now - last_progress_tick) >= polling_interval:
                    delta = self._read_delta(ctx, entry)
                    if delta:
                        ctx.emit(BackgroundEvent(
                            tid=tid, kind="progress", entry=entry, delta_text=delta,
                        ))
                    last_progress_tick = now
        except asyncio.CancelledError:
            return

    def _read_delta(self, ctx: BackgroundContext, entry: PanelEntry) -> str:
        if not entry.output_file:
            return ""
        output_path = Path(entry.output_file)
        if not output_path.exists():
            return ""
        try:
            with output_path.open("rb") as f:
                f.seek(entry.last_delivered_bytes)
                delta_bytes = f.read()
        except OSError:
            return ""
        if not delta_bytes:
            return ""
        entry.last_delivered_bytes += len(delta_bytes)
        ctx.save_entry(entry)
        return delta_bytes.decode(errors="replace")


# ── Manager ──────────────────────────────────────────────────────────────────


class BackgroundTaskManager:
    """Per-session manager for non-blocking tool calls.

    Args:
        panel_dir: `sessions/<id>/core/panel/` — where panel entry JSON files live.
        tool_results_dir: `_sessions/<id>/tool_results/` — where output files live.
        venv_env_provider: Callable returning an env dict to apply when spawning
            subprocesses (for venv activation). None = inherit the parent env.

    Bash is registered automatically as the default runner; sessions can call
    `register_runner(name, runner)` to add more (e.g. sub_agent).
    """

    # Bound on the event queue so a stalled/dead daemon loop can't OOM us.
    # Drop-oldest policy: we prefer the fresh events when a slow consumer
    # falls behind. 1024 is ~hours of activity at reasonable polling rates.
    _EVENT_QUEUE_MAXSIZE = 1024

    def __init__(
        self,
        panel_dir: Path,
        tool_results_dir: Path,
        venv_env_provider: Callable[[], dict[str, str] | None] | None = None,
        guardian: "object | None" = None,
    ) -> None:
        self._panel_dir = panel_dir
        self._tool_results_dir = tool_results_dir
        self._venv_env_provider = venv_env_provider
        self._tasks: dict[str, asyncio.Task] = {}
        self._events: asyncio.Queue[BackgroundEvent] = asyncio.Queue(
            maxsize=self._EVENT_QUEUE_MAXSIZE
        )
        self._dropped_events = 0
        self._runners: dict[str, BackgroundRunner] = {}
        self._ctx = BackgroundContext(
            panel_dir=panel_dir,
            tool_results_dir=tool_results_dir,
            venv_env_provider=venv_env_provider,
            _emit=self._emit_event,
            guardian=guardian,
        )
        # Auto-register bash so existing callers (Session, agent.py) work
        # without explicit registration.
        self.register_runner("bash", BashRunner())

    def _emit_event(self, evt: BackgroundEvent) -> None:
        """Non-blocking enqueue with drop-oldest policy when saturated."""
        try:
            self._events.put_nowait(evt)
            return
        except asyncio.QueueFull:
            try:
                self._events.get_nowait()
                self._dropped_events += 1
            except asyncio.QueueEmpty:
                pass
            try:
                self._events.put_nowait(evt)
            except asyncio.QueueFull:
                self._dropped_events += 1

    @property
    def events(self) -> asyncio.Queue[BackgroundEvent]:
        return self._events

    @property
    def context(self) -> BackgroundContext:
        """Exposed so runners can be tested in isolation against a real ctx."""
        return self._ctx

    # ── Runner registry ──────────────────────────────────────────────────

    def register_runner(self, tool_name: str, runner: BackgroundRunner) -> None:
        """Bind `tool_name` to `runner`. Re-registering replaces the old runner."""
        self._runners[tool_name] = runner

    def runner_for(self, tool_name: str) -> BackgroundRunner | None:
        return self._runners.get(tool_name)

    # ── Public API ───────────────────────────────────────────────────────

    async def spawn(
        self,
        tool_name: str,
        input: dict[str, Any],
        polling_interval: int | None = None,
        *,
        entry_type: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> str:
        """Spawn a backgroundable tool. Returns the tid immediately.

        Raises ValueError if no runner is registered for `tool_name` or if the
        runner rejects `input` synchronously.
        """
        runner = self._runners.get(tool_name)
        if runner is None:
            raise ValueError(
                f"BackgroundTaskManager.spawn: no runner registered for {tool_name!r}"
            )
        # Synchronous input validation. Lets callers see config errors at the
        # spawn site, not as silent dead panel entries.
        runner.validate(input)

        if entry_type is None:
            # Sub-agent entries get a dedicated panel type so the UI can
            # render the child-session card differently.
            from butterfly.session_engine.panel import TYPE_SUB_AGENT
            entry_type = TYPE_SUB_AGENT if tool_name == "sub_agent" else TYPE_PENDING_TOOL
        entry = create_pending_tool_entry(
            self._panel_dir,
            tool_name=tool_name,
            input=dict(input),  # copy so later mutations don't leak
            polling_interval=polling_interval,
            entry_type=entry_type,
            meta=meta,
        )

        task = asyncio.create_task(
            self._run_with_runner(runner, entry.tid, dict(input), polling_interval),
            name=f"bgtask_{entry.tid}",
        )
        self._tasks[entry.tid] = task
        return entry.tid

    async def kill(self, tid: str) -> bool:
        """Kill the runner's work for `tid`. Returns True if it was running."""
        entry = load_entry(self._panel_dir, tid)
        if entry is None or entry.is_terminal():
            return False
        runner = self._runners.get(entry.tool_name)
        if runner is None:
            # Unknown runner — still mark the entry as killed so UIs converge.
            entry.status = STATUS_KILLED
            entry.finished_at = time.time()
            save_entry(self._panel_dir, entry)
            return True
        return await runner.kill(self._ctx, tid)

    def sweep_restart(self) -> list[PanelEntry]:
        """Mark all `running` entries as killed_by_restart and emit events.

        Call once at server/daemon init.
        """
        updated = sweep_killed_by_restart(self._panel_dir)
        for entry in updated:
            self._emit_event(BackgroundEvent(
                tid=entry.tid, kind="killed_by_restart", entry=entry,
            ))
        return updated

    async def shutdown(self) -> None:
        """Cancel all in-flight tasks (subprocesses are left alone — they
        will be reaped or marked killed_by_restart on next startup)."""
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    # ── Internals ────────────────────────────────────────────────────────

    async def _run_with_runner(
        self,
        runner: BackgroundRunner,
        tid: str,
        input: dict[str, Any],
        polling_interval: int | None,
    ) -> None:
        """Drive a single runner.run() to completion + fire terminal event."""
        entry = load_entry(self._panel_dir, tid)
        if entry is None:
            return
        try:
            exit_code = await runner.run(self._ctx, tid, entry, input, polling_interval)
        except asyncio.CancelledError:
            self._transition_terminal(tid, STATUS_KILLED, exit_code=None)
            raise
        except Exception as exc:  # noqa: BLE001 — runner failures must not crash session
            output_path = self._tool_results_dir / f"{tid}.txt"
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(f"[runner crashed: {exc!r}]", encoding="utf-8")
            except OSError:
                pass
            self._transition_terminal(tid, STATUS_COMPLETED, exit_code=-1)
            return

        # Resolve final status — kill() may have set STATUS_KILLED concurrently.
        cur = load_entry(self._panel_dir, tid)
        final_status = (
            STATUS_KILLED if cur is not None and cur.status == STATUS_KILLED
            else STATUS_COMPLETED
        )
        self._transition_terminal(tid, final_status, exit_code=exit_code)

    def _transition_terminal(
        self, tid: str, status: str, *, exit_code: int | None
    ) -> None:
        entry = load_entry(self._panel_dir, tid)
        if entry is None:
            return
        entry.status = status
        entry.exit_code = exit_code
        entry.finished_at = time.time()
        output_path = Path(entry.output_file) if entry.output_file else None
        if output_path and output_path.exists():
            try:
                entry.output_bytes = output_path.stat().st_size
            except OSError:
                pass
        save_entry(self._panel_dir, entry)
        self._emit_event(BackgroundEvent(
            tid=tid, kind="completed", entry=entry,
        ))
