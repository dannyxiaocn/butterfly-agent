"""Bash/CLI execution tool for nutshell agents.

Two execution modes:
  - subprocess (default): asyncio.create_subprocess_shell — async, portable
  - pty: pseudo-terminal via stdlib pty + thread executor — preserves isatty(),
    color output, and avoids stdout buffering. Unix only.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from nutshell.tool_engine.executor.base import BaseExecutor

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")
_MAX_OUTPUT = 10_000

_REPO_ROOT = Path(__file__).parent.parent.parent


def _venv_env() -> dict[str, str] | None:
    """Build an env dict with session venv activated, or None if no venv."""
    session_id = os.environ.get("NUTSHELL_SESSION_ID", "")
    if not session_id:
        return None
    venv_path = _REPO_ROOT / "sessions" / session_id / ".venv"
    if not venv_path.is_dir():
        return None
    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(venv_path)
    env["PATH"] = str(venv_path / "bin") + ":" + env.get("PATH", "")
    env.pop("PYTHONHOME", None)
    return env


# -- subprocess mode -----------------------------------------------------------

async def _run_subprocess(
    command: str,
    timeout: float,
    workdir: str | None,
    max_output: int,
) -> str:
    env = _venv_env()
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=workdir,
        env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"[timed out after {timeout}s]"

    output = stdout.decode(errors="replace")
    if len(output) > max_output:
        output = output[-max_output:]
        output = f"[...truncated]\n{output}"
    return f"{output.rstrip()}\n[exit {proc.returncode}]"


# -- PTY mode ------------------------------------------------------------------

def _run_pty_sync(command: str, timeout: float, workdir: str | None, max_output: int) -> str:
    """Run command in a PTY (blocking). Called via run_in_executor.

    Reader pattern: dedicated thread does blocking os.read() on master_fd.
    Main thread waits for proc with timeout, then closes master_fd to unblock
    the reader thread via EIO — reliable on both macOS and Linux.
    """
    import threading

    try:
        master_fd, slave_fd = os.openpty()
    except OSError as exc:
        return f"[pty unavailable: {exc}]"

    chunks: list[bytes] = []
    read_done = threading.Event()

    def _reader() -> None:
        while True:
            try:
                data = os.read(master_fd, 4096)
                if not data:
                    break
                chunks.append(data)
            except OSError:
                break  # EIO after slave closed, or master_fd was force-closed
        read_done.set()

    env = _venv_env()
    try:
        try:
            proc = subprocess.Popen(
                ["bash", "-c", command],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                cwd=workdir,
                env=env,
            )
        finally:
            os.close(slave_fd)

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            timed_out = True

        # Wait for reader to drain remaining output (up to 0.5s)
        read_done.wait(timeout=0.5)

    finally:
        # Force-close master_fd — if reader is still blocked on os.read(),
        # closing the fd raises OSError(EBADF) in that thread, unblocking it.
        try:
            os.close(master_fd)
        except OSError:
            pass

    raw = b"".join(chunks).decode(errors="replace")
    output = _ANSI_RE.sub("", raw)  # strip ANSI escape codes
    if len(output) > max_output:
        output = output[-max_output:]
        output = f"[...truncated]\n{output}"
    suffix = "\n[timed out]" if timed_out else f"\n[exit {proc.returncode}]"
    return output.rstrip() + suffix


async def _run_pty(
    command: str,
    timeout: float,
    workdir: str | None,
    max_output: int,
) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _run_pty_sync, command, timeout, workdir, max_output
    )


# -- Executor class ------------------------------------------------------------

class BashExecutor(BaseExecutor):
    """Executor for the built-in bash tool."""

    def __init__(
        self,
        timeout: float = 30.0,
        workdir: str | None = None,
        max_output: int = _MAX_OUTPUT,
    ) -> None:
        self._timeout = timeout
        self._workdir = workdir
        self._max_output = max_output

    @classmethod
    def can_handle(cls, tool_name: str, tool_path: Path | None) -> bool:
        return tool_name == "bash"

    async def execute(self, **kwargs: Any) -> str:
        command: str = kwargs["command"]
        timeout = float(kwargs.get("timeout") or self._timeout)
        workdir = kwargs.get("workdir") or self._workdir
        pty = bool(kwargs.get("pty", False))
        if pty:
            return await _run_pty(command, timeout, workdir, self._max_output)
        return await _run_subprocess(command, timeout, workdir, self._max_output)
