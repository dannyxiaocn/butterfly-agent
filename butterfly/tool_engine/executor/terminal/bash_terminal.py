"""Built-in bash execution tool for butterfly agents.

One-shot subprocess per call — no PTY, no shared state. For persistent shell
sessions use `session_shell`. For long-running commands use `run_in_background=true`
(routed through `BackgroundTaskManager` at the agent-loop layer).

Structured output:

    <stdout/stderr combined>
    [exit N, duration T.Ts, truncated bool]
    [spilled: <path>]        # only when output > max_output_chars

See `docs/butterfly/tool_engine/design.md` §3.1.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from pathlib import Path
from typing import Any

from butterfly.core.guardian import Guardian
from butterfly.core.tool import Tool
from butterfly.tool_engine.executor.base import BaseExecutor

_MAX_OUTPUT = 10_000

_REPO_ROOT = Path(__file__).parent.parent.parent.parent


def _venv_env() -> dict[str, str] | None:
    """Build an env dict with session venv activated, or None if no venv."""
    session_id = os.environ.get("BUTTERFLY_SESSION_ID", "")
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


def _spill_if_oversized(
    output: str, max_output: int, tool_results_dir: Path | None
) -> tuple[str, bool, Path | None]:
    """If output exceeds max_output, write full output to disk and return
    (truncated_tail, truncated=True, spill_path). Otherwise return (output, False, None).
    """
    if len(output) <= max_output:
        return output, False, None
    if tool_results_dir is None:
        return output[-max_output:], True, None
    tool_results_dir.mkdir(parents=True, exist_ok=True)
    spill_path = tool_results_dir / f"bash_{secrets.token_hex(4)}.txt"
    spill_path.write_text(output, encoding="utf-8")
    return output[-max_output:], True, spill_path


async def _run_subprocess(
    command: str,
    timeout: float,
    workdir: str | None,
    stdin: str | None,
    max_output: int,
    tool_results_dir: Path | None,
    env_extras: dict[str, str] | None = None,
) -> str:
    env = _venv_env()
    if env_extras:
        if env is None:
            env = os.environ.copy()
        env.update(env_extras)
    proc = await asyncio.create_subprocess_shell(
        command,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=workdir,
        env=env,
    )
    started = time.monotonic()
    try:
        stdout_bytes, _ = await asyncio.wait_for(
            proc.communicate(input=stdin.encode() if stdin is not None else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        duration = time.monotonic() - started
        return f"[timed out after {timeout}s, duration {duration:.1f}s]"

    duration = time.monotonic() - started
    output = stdout_bytes.decode(errors="replace").rstrip()
    tail, truncated, spill_path = _spill_if_oversized(output, max_output, tool_results_dir)
    footer = f"[exit {proc.returncode}, duration {duration:.1f}s, truncated {str(truncated).lower()}]"
    if spill_path is not None:
        footer += f"\n[spilled: {spill_path}]"
    body = f"[...truncated to last {max_output} chars]\n{tail}" if truncated else tail
    return f"{body}\n{footer}" if body else footer


class BashExecutor(BaseExecutor):
    """Executor for the built-in bash tool.

    v2.0.5 — subprocess only. PTY mode was removed; interactive prompts are
    handled by (a) the `stdin` parameter (pre-feed answers), (b) the stall
    watchdog on backgrounded tasks, or (c) `session_shell` for multi-step
    workflows that need persistent state.
    """

    def __init__(
        self,
        timeout: float = 30.0,
        workdir: str | None = None,
        max_output: int = _MAX_OUTPUT,
        tool_results_dir: Path | None = None,
        guardian: Guardian | None = None,
    ) -> None:
        self._timeout = timeout
        self._workdir = workdir
        self._max_output = max_output
        self._tool_results_dir = tool_results_dir
        self._guardian = guardian

    async def execute(self, **kwargs: Any) -> str:
        command: str = kwargs["command"]
        timeout = float(kwargs.get("timeout") or self._timeout)
        workdir = kwargs.get("workdir") or self._workdir
        stdin = kwargs.get("stdin")
        env_extras: dict[str, str] | None = None
        if self._guardian is not None:
            # Pin cwd to the guardian root so command-relative paths can't
            # escape, and surface the boundary as an env var the agent's
            # mode prompt explicitly references.
            workdir = str(self._guardian.root)
            env_extras = {"BUTTERFLY_GUARDIAN_ROOT": str(self._guardian.root)}
        # run_in_background / polling_interval are consumed by the agent loop
        # before reaching the executor, so we simply ignore them here.
        return await _run_subprocess(
            command,
            timeout,
            workdir,
            stdin,
            self._max_output,
            self._tool_results_dir,
            env_extras,
        )


def create_bash_tool(
    timeout: float = 30.0,
    workdir: str | None = None,
    max_output: int = _MAX_OUTPUT,
    tool_results_dir: Path | None = None,
    guardian: Guardian | None = None,
) -> Tool:
    """Return a bash Tool pre-configured with defaults.

    The agent can override timeout per call. workdir is auto-injected from the
    session directory and agents should always use relative paths.
    """
    executor = BashExecutor(
        timeout=timeout,
        workdir=workdir,
        max_output=max_output,
        tool_results_dir=tool_results_dir,
        guardian=guardian,
    )

    async def bash(**kwargs: Any) -> str:
        return await executor.execute(**kwargs)

    # Explicit schema — the ToolLoader / Tool will auto-inject run_in_background
    # and polling_interval when backgroundable=True.
    schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds. Omit to use the default (30s).",
            },
            "stdin": {
                "type": "string",
                "description": (
                    "Optional string piped to the command's stdin. Useful for "
                    "pre-feeding interactive prompts (e.g. 'y\\n' for apt/brew)."
                ),
            },
        },
        "required": ["command"],
    }

    description = (
        "Execute a shell command. Each call spawns a fresh subprocess — `cd`, "
        "`export`, and aliases DO NOT persist across calls. Use relative paths "
        "(resolved against your session workdir). For multi-step workflows that "
        "need shared environment, use `session_shell` instead."
    )

    return Tool(
        name="bash",
        description=description,
        func=bash,
        schema=schema,
        backgroundable=True,
    )
