"""Built-in bash/CLI execution tool for nutshell agents.

Provides create_bash_tool(), a factory that returns a Tool the agent can call
to run arbitrary shell commands.

Two execution modes:
  - subprocess (default): asyncio.create_subprocess_shell — async, portable
  - pty: pseudo-terminal via stdlib pty + thread executor — preserves isatty(),
    color output, and avoids stdout buffering. Unix only.

Sandbox:
  Commands are checked against DANGEROUS_DEFAULTS + any extra blocked_patterns
  before execution.  Blocked commands return an error message without being run.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional, Sequence

from nutshell.core.tool import Tool
from nutshell.tool_engine.executor.base import BaseExecutor
from nutshell.tool_engine.sandbox import check_blocked

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")
_MAX_OUTPUT = 10_000


# ── subprocess mode ────────────────────────────────────────────────────────────

async def _run_subprocess(
    command: str,
    timeout: float,
    workdir: str | None,
    max_output: int,
) -> str:
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=workdir,
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


# ── PTY mode ───────────────────────────────────────────────────────────────────

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
                if data:
                    chunks.append(data)
            except OSError:
                break  # EIO after slave closed, or master_fd was force-closed
        read_done.set()

    try:
        proc = subprocess.Popen(
            ["bash", "-c", command],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            cwd=workdir,
        )
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
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _run_pty_sync, command, timeout, workdir, max_output
    )


# ── Executor class ─────────────────────────────────────────────────────────────

class BashExecutor(BaseExecutor):
    """Executor for the built-in bash tool."""

    def __init__(
        self,
        timeout: float = 30.0,
        workdir: str | None = None,
        max_output: int = _MAX_OUTPUT,
        blocked_patterns: Sequence[str] | None = None,
    ) -> None:
        self._timeout = timeout
        self._workdir = workdir
        self._max_output = max_output
        self._blocked_patterns = list(blocked_patterns) if blocked_patterns else []

    @classmethod
    def can_handle(cls, tool_name: str, tool_path: Path | None) -> bool:
        return tool_name == "bash"

    async def execute(self, **kwargs: Any) -> str:
        command: str = kwargs["command"]

        # Sandbox check — reject dangerous commands before execution
        violation = check_blocked(command, self._blocked_patterns or None)
        if violation is not None:
            return f"[SANDBOX] {violation}\nCommand was NOT executed."

        timeout = float(kwargs.get("timeout") or self._timeout)
        workdir = kwargs.get("workdir") or self._workdir
        pty = bool(kwargs.get("pty", False))
        if pty:
            return await _run_pty(command, timeout, workdir, self._max_output)
        return await _run_subprocess(command, timeout, workdir, self._max_output)


# ── Factory ────────────────────────────────────────────────────────────────────

def create_bash_tool(
    timeout: float = 30.0,
    workdir: str | None = None,
    max_output: int = _MAX_OUTPUT,
    blocked_patterns: Sequence[str] | None = None,
) -> Tool:
    """Return a bash Tool pre-configured with defaults.

    The agent can override timeout and workdir per call.

    Args:
        timeout: Default execution timeout in seconds.
        workdir: Default working directory (None = inherit from process).
        max_output: Max characters of output returned to the model.
        blocked_patterns: Extra regex patterns to block (on top of DANGEROUS_DEFAULTS).
                          Loaded from params.json "blocked_patterns" at session level.
    """
    executor = BashExecutor(
        timeout=timeout,
        workdir=workdir,
        max_output=max_output,
        blocked_patterns=blocked_patterns,
    )

    async def bash(
        command: str,
        timeout: Optional[float] = None,
        workdir: Optional[str] = None,
        pty: Optional[bool] = False,
    ) -> str:
        """Execute a shell command and return stdout+stderr combined.

        Args:
            command: The shell command to run (passed to bash -c).
            timeout: Execution timeout in seconds. Defaults to factory setting.
            workdir: Working directory. Defaults to factory setting.
            pty: If true, run in a pseudo-terminal (preserves color, isatty).
                 Unix only. Useful for commands that buffer output differently
                 or check terminal width.
        """
        return await executor.execute(command=command, timeout=timeout, workdir=workdir, pty=pty)

    return Tool(
        name="bash",
        description=(
            "Execute a shell command. Returns stdout+stderr combined and exit code. "
            "Use pty=true for commands that need an interactive terminal (color output, "
            "progress bars)."
        ),
        func=bash,
        schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                "timeout": {
                    "type": "number",
                    "description": "Timeout in seconds. Omit to use the default.",
                },
                "workdir": {
                    "type": "string",
                    "description": "Working directory path. Omit to use the default.",
                },
                "pty": {
                    "type": "boolean",
                    "description": (
                        "Run in a pseudo-terminal. Preserves color output and isatty(). "
                        "Unix only. Default false."
                    ),
                },
            },
            "required": ["command"],
        },
    )
