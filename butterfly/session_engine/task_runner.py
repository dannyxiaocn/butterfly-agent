"""Execute task card trigger/end scripts.

Separated from ``task_cards.py`` so the pure dataclass / file-ops layer
stays dependency-free. ``run_check`` handles the async subprocess, timeout,
stdout capture, and last-line parsing; the caller decides what to do with
the result (enqueue, mark_finished, emit error event, etc.).
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

from butterfly.session_engine.task_cards import (
    ScriptResult,
    parse_end_output,
    parse_trigger_output,
)

_CHECK_TIMEOUT_SEC = 10.0
_OUTPUT_CAP = 4000  # bytes retained from each stream


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the subprocess and everything it spawned.

    We launch the script via ``start_new_session=True`` so the bash
    invocation lives in its own process group. A plain ``proc.kill()``
    only targets the direct bash PID — any helper commands it backgrounded
    (``sleep 100 &``, child scripts, etc.) would be left as orphans
    reparented to init. Using ``os.killpg`` on the group reaps them too.
    Race-safe: ``ProcessLookupError`` means the process already exited
    cleanly before we fired the signal.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def run_script(script: Path, *, cwd: Path) -> ScriptResult:
    """Run a trigger/end script and capture its result.

    Parsing the tag is left to the caller (``parse_trigger_output`` /
    ``parse_end_output``) since the two scripts share the plumbing but
    accept different markers.
    """
    started = time.monotonic()
    stdout_text = ""
    stderr_text = ""
    exit_code: int | None = None
    timed_out = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", str(script),
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(),
                timeout=_CHECK_TIMEOUT_SEC,
            )
            exit_code = proc.returncode
            stdout_text = out.decode("utf-8", errors="replace")[-_OUTPUT_CAP:]
            stderr_text = err.decode("utf-8", errors="replace")[-_OUTPUT_CAP:]
        except asyncio.TimeoutError:
            timed_out = True
            _kill_process_group(proc)
            try:
                out, err = await proc.communicate()
                stdout_text = out.decode("utf-8", errors="replace")[-_OUTPUT_CAP:]
                stderr_text = err.decode("utf-8", errors="replace")[-_OUTPUT_CAP:]
            except Exception:
                pass
    except FileNotFoundError:
        stderr_text = "bash not found"

    duration_ms = int((time.monotonic() - started) * 1000)
    # The outer caller parses tag/message after selecting trigger vs end
    # semantics. We still fill them here using a union-style parse so the
    # ScriptResult remains self-sufficient — callers can inspect either
    # marker set without re-walking stdout.
    parsed = (
        parse_trigger_output(stdout_text, exit_code)
        or parse_end_output(stdout_text, exit_code)
    )
    tag, message = (parsed if parsed is not None else (None, ""))
    return ScriptResult(
        tag=tag,
        message=message,
        stdout=stdout_text,
        stderr=stderr_text,
        exit_code=exit_code,
        duration_ms=duration_ms,
        timed_out=timed_out,
    )
