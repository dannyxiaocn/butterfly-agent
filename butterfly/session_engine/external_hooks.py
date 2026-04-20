"""External user-configurable hooks.

Agents (and users) drop a single ``main.sh`` under
``sessions/<id>/core/hook/<event>/`` to react to session lifecycle events.
Three events are supported:

    session_start     — fired once when the daemon starts a Session
    agent_loop_start  — fired before every Agent.run invocation (user chat,
                        task wakeup, bg-tool notification — any path that
                        enters the agent loop)
    agent_loop_end    — fired after every Agent.run completes or is cancelled

We only look for ``<event>/main.sh`` per fire. Agents can stash as many
helper scripts alongside it as they want, but ordering and conditional
execution are expressed inside ``main.sh`` (plain bash). This keeps the
harness behaviour predictable — one timeout, one output stream, one exit
code — while letting agents orchestrate arbitrarily.

Script contract
---------------
    * stdin: a JSON object ``{event, session_id, data}`` (``data`` carries
      event-specific fields)
    * working directory: the session root (``sessions/<id>/``)
    * 30 s hard timeout; SIGKILL on timeout. Detach long work with ``&``
      inside main.sh if you need it to outlive the hook.
    * stdout + stderr are **observed only** — exit code is logged but
      does NOT block or alter the agent loop (v2.0.27 baseline). A future
      release may add a blocking mode; see TODO(blocking-hooks).

TODO(blocking-hooks): add a per-event opt-in so exit != 0 can cancel the
impending agent_loop_start or the impending session_start work. Will need
a deliberate decision on which events can block (agent_loop_end probably
shouldn't — the loop has already ended) and a way for the script to
surface a user-visible rejection reason via stderr.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

_HOOK_TIMEOUT_SEC = 30.0
_OUTPUT_CAP = 4000  # bytes captured per stream

EMIT_EVENT = Callable[[dict], None]
EmitEventAsync = Callable[[dict], Awaitable[None] | None]

VALID_EVENTS = frozenset({"session_start", "agent_loop_start", "agent_loop_end"})

_MAIN_SCRIPT = "main.sh"


def main_script_path(hook_dir: Path, event: str) -> Path:
    return hook_dir / event / _MAIN_SCRIPT


async def run_hooks(
    event: str,
    data: dict,
    *,
    hook_dir: Path,
    session_id: str,
    cwd: Path,
    emit_event: EMIT_EVENT,
) -> None:
    """Run ``<event>/main.sh`` (if present).

    Per-script errors, timeouts, and non-zero exits are swallowed — each
    run is reported via ``emit_event`` as a ``hook_run`` entry so the UI
    and operator can see what happened without the agent loop being
    affected.
    """
    if event not in VALID_EVENTS:
        return
    script = main_script_path(hook_dir, event)
    if not script.is_file():
        return

    payload = {"event": event, "session_id": session_id, "data": data or {}}
    stdin_bytes = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

    started = time.monotonic()
    exit_code: int | None = None
    stdout_tail = ""
    stderr_tail = ""
    timed_out = False
    error: str | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", str(script),
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # start_new_session puts bash in its own process group so
            # os.killpg can reap every helper it spawned if we timeout.
            # A plain proc.kill() would leave backgrounded children (e.g.
            # `sleep 100 &`) as orphans reparented to init.
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin_bytes),
                timeout=_HOOK_TIMEOUT_SEC,
            )
            exit_code = proc.returncode
            stdout_tail = out.decode("utf-8", errors="replace")[-_OUTPUT_CAP:]
            stderr_tail = err.decode("utf-8", errors="replace")[-_OUTPUT_CAP:]
        except asyncio.TimeoutError:
            timed_out = True
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                out, err = await proc.communicate()
                stdout_tail = out.decode("utf-8", errors="replace")[-_OUTPUT_CAP:]
                stderr_tail = err.decode("utf-8", errors="replace")[-_OUTPUT_CAP:]
            except Exception:
                pass
    except FileNotFoundError:
        error = "bash not found"
    except Exception as exc:  # pragma: no cover - defensive
        error = f"{type(exc).__name__}: {exc}"

    duration_ms = int((time.monotonic() - started) * 1000)
    report: dict[str, Any] = {
        "type": "hook_run",
        "event": event,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
    }
    if timed_out:
        report["timed_out"] = True
    if stdout_tail:
        report["stdout"] = stdout_tail
    if stderr_tail:
        report["stderr"] = stderr_tail
    if error:
        report["error"] = error
    try:
        emit_event(report)
    except Exception:
        pass
