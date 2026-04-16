"""Persistent per-session bash shell (session_shell tool executor).

One long-lived `bash --norc --noprofile` subprocess per executor instance.
Lazy-started on first call, reused thereafter; `cd`, `export`, aliases, and
functions persist between calls.

Sentinel protocol
-----------------
To know when a command finishes (and capture its exit code) we wrap every
invocation like this on stdin::

    <command>
    printf "\n__<marker>_%d__\n" $?

where `<marker>` is a per-call random token `BFY_DONE_<hex>`. We then read
merged stdout+stderr (spawn uses ``stderr=STDOUT``) line by line until we see
``__<marker>_<exit>__``; everything before is the command's output, and the
regex group is the exit code. The marker is randomized per call so user
output is extremely unlikely to collide with it.

Timeout recovery
----------------
``asyncio.wait_for`` guards the read loop. On timeout we send ``SIGINT`` to
the shell's process group (shell spawned with ``start_new_session=True``);
that should interrupt the foreground command and return control to the
prompt. We give the shell 2s to print the sentinel on its own; if it doesn't,
we ``SIGKILL`` and mark the shell dead so the next call lazy-restarts it.
Timeout result includes ``[timed out after Ns, shell restarted]`` + whatever
partial output we already read + ``[exit unknown]``.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import signal
import subprocess
import time
from collections import deque
from typing import Any, Callable, Optional

from butterfly.core.guardian import Guardian
from butterfly.tool_engine.executor.base import BaseExecutor

_MAX_OUTPUT = 10_000
# Ring-buffer the incoming lines at 2x the cap so we can cheaply keep the
# trailing `_MAX_OUTPUT` chars without letting a `yes`-style firehose OOM us.
_BUFFER_FACTOR = 2


class SessionShellExecutor(BaseExecutor):
    """Executor backing the `session_shell` tool.

    See module docstring for the sentinel protocol and timeout handling.
    """

    def __init__(
        self,
        workdir: str | None = None,
        venv_env_provider: Optional[Callable[[], Optional[dict[str, str]]]] = None,
        max_output: int = _MAX_OUTPUT,
        guardian: Guardian | None = None,
    ) -> None:
        # Guardian (sub-agent explorer mode) overrides workdir to the
        # boundary root so the persistent shell can never start outside
        # the sandbox. Without this, an explorer-mode child would reach
        # session_shell unconstrained — equivalent to a Guardian bypass.
        # (PR #28 review Bug #4.)
        self._guardian = guardian
        if guardian is not None:
            workdir = str(guardian.root)
        self._workdir = workdir
        self._venv_env_provider = venv_env_provider
        self._max_output = max_output
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._ever_spawned = False

    # -- lifecycle -------------------------------------------------------------

    def _build_env(self) -> dict[str, str]:
        env: dict[str, str] | None = None
        if self._venv_env_provider is not None:
            try:
                env = self._venv_env_provider()
            except Exception:
                env = None
        if env is None:
            env = os.environ.copy()
        # Don't pollute ~/.bash_history with agent commands
        env["HISTFILE"] = ""
        # Surface the Guardian boundary so the agent's mode prompt can
        # reason about it from inside the persistent shell.
        if self._guardian is not None:
            env["BUTTERFLY_GUARDIAN_ROOT"] = str(self._guardian.root)
        return env

    async def _spawn(self) -> None:
        """Spawn a fresh `bash --norc --noprofile` subprocess."""
        env = self._build_env()
        self._proc = await asyncio.create_subprocess_exec(
            "bash",
            "--norc",
            "--noprofile",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self._workdir,
            env=env,
            start_new_session=True,  # new process group so killpg(SIGINT) works
        )
        self._ever_spawned = True
        # Defensive: drain any startup chatter. With --norc --noprofile there
        # should be none, but give the shell a moment and then skip anything
        # that's immediately readable.
        await asyncio.sleep(0.05)
        await self._drain_available()

    async def _drain_available(self) -> None:
        """Non-blocking: consume whatever is immediately available on stdout."""
        if self._proc is None or self._proc.stdout is None:
            return
        try:
            while True:
                # Read with a tiny timeout; if nothing is pending, stop.
                chunk = await asyncio.wait_for(self._proc.stdout.read(4096), timeout=0.01)
                if not chunk:
                    return
        except asyncio.TimeoutError:
            return

    def _is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _ensure_alive(self) -> tuple[bool, bool]:
        """Ensure shell is running.

        Returns (spawned, was_restart):
          - spawned: True if we had to spawn it this call.
          - was_restart: True only if we have previously spawned a shell in
            this executor; i.e. this is a *re*start, not the first lazy
            spawn. Callers prepend `[shell restarted]` only when was_restart
            is True.
        """
        if self._is_alive():
            return False, False
        was_restart = self._ever_spawned
        await self._spawn()
        return True, was_restart

    async def _hard_kill(self) -> None:
        """Best-effort: SIGTERM → 0.5s grace → SIGKILL. Wait for exit."""
        if self._proc is None:
            return
        if self._proc.returncode is not None:
            self._proc = None
            return
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await self._proc.wait()
            except Exception:
                pass
        self._proc = None

    # -- main entry point ------------------------------------------------------

    async def execute(self, **kwargs: Any) -> str:
        command = kwargs.get("command")
        if not isinstance(command, str):
            return "Error: `command` (string) is required.\n[exit unknown]"
        # Explicit None check so `timeout=0` isn't silently coerced to 60 by
        # the `or` idiom — 0 is a valid (though cruel) caller intent.
        raw_timeout = kwargs.get("timeout")
        if raw_timeout is None:
            timeout = 60.0
        else:
            try:
                timeout = float(raw_timeout)
            except (TypeError, ValueError):
                timeout = 60.0
        reset = bool(kwargs.get("reset", False))

        async with self._lock:
            if reset:
                await self._hard_kill()
                # Spawn eagerly so the next call doesn't also pay the cost.
                await self._spawn()
                return "[shell reset]\n[exit 0]"

            _, was_restart = await self._ensure_alive()
            prefix = "[shell restarted]\n" if was_restart else ""

            return await self._run_one(command, timeout, prefix)

    # -- single-command run ----------------------------------------------------

    async def _run_one(self, command: str, timeout: float, prefix: str) -> str:
        assert self._proc is not None and self._proc.stdin is not None and self._proc.stdout is not None
        marker = f"BFY_DONE_{secrets.token_hex(4)}"
        pattern = re.compile(rf"__{re.escape(marker)}_(-?\d+)__")

        payload = (
            command
            + "\n"
            + f'printf "\\n__{marker}_%d__\\n" $?\n'
        )
        start = time.monotonic()

        try:
            self._proc.stdin.write(payload.encode())
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            # Shell died between _ensure_alive and now; mark dead and restart
            self._proc = None
            await self._spawn()
            return prefix + "[shell died before command; restarted]\n[exit unknown]"

        # Ring-buffer output lines with a soft cap on total length. A command
        # like `yes | head -c 1G` would otherwise fill memory; here we drop
        # the head once we exceed 2× the final output cap, preserving the
        # tail (which is what `_cap` would keep anyway).
        collected: "deque[str]" = deque()
        collected_len = 0
        buffer_cap = max(self._max_output * _BUFFER_FACTOR, self._max_output)

        def _append(line: str) -> None:
            nonlocal collected_len
            collected.append(line)
            collected_len += len(line)
            while collected_len > buffer_cap and collected:
                popped = collected.popleft()
                collected_len -= len(popped)

        async def _read_until_marker() -> int | None:
            """Read lines until the sentinel is seen; return exit code or None."""
            assert self._proc is not None and self._proc.stdout is not None
            while True:
                line_bytes = await self._proc.stdout.readline()
                if not line_bytes:
                    return None  # EOF: shell died
                line = line_bytes.decode(errors="replace")
                m = pattern.search(line)
                if m:
                    # Strip the sentinel line from output; anything before the
                    # match on that same line (shouldn't happen, our printf
                    # emits it on its own line) gets preserved.
                    before = line[: m.start()]
                    if before and before != "\n":
                        _append(before)
                    return int(m.group(1))
                _append(line)

        try:
            exit_code = await asyncio.wait_for(_read_until_marker(), timeout=timeout)
        except asyncio.TimeoutError:
            # Try SIGINT the process group; give the shell a chance to recover
            # and emit the sentinel on its own.
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGINT)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            recovered_exit: int | None = None
            try:
                rc = await asyncio.wait_for(_read_until_marker(), timeout=2.0)
                # Only truly recovered if we got a real exit code. EOF (None)
                # means shell died from the SIGINT.
                if rc is not None:
                    recovered_exit = rc
            except asyncio.TimeoutError:
                recovered_exit = None

            if recovered_exit is None:
                await self._hard_kill()
                await self._spawn()
                body = "".join(collected).rstrip()
                header = f"[timed out after {timeout}s, shell restarted]"
                out = self._cap(f"{header}\n{body}".rstrip())
                return prefix + f"{out}\n[exit unknown]"
            exit_code = recovered_exit

            # SIGINT successfully returned control; shell is still alive, but
            # the command was interrupted. Report as timeout anyway for
            # predictability.
            duration = time.monotonic() - start
            body = "".join(collected).rstrip()
            header = f"[timed out after {timeout}s, interrupted]"
            out = self._cap(f"{header}\n{body}".rstrip())
            return prefix + f"{out}\n[exit {exit_code}, duration {duration:.1f}s]"

        if exit_code is None:
            # EOF before sentinel — shell died mid-command.
            self._proc = None
            body = "".join(collected).rstrip()
            out = self._cap(body)
            return prefix + f"{out}\n[shell died]\n[exit unknown]"

        duration = time.monotonic() - start
        body = "".join(collected).rstrip()
        out = self._cap(body)
        return prefix + f"{out}\n[exit {exit_code}, duration {duration:.1f}s]"

    def _cap(self, text: str) -> str:
        if len(text) > self._max_output:
            tail = text[-self._max_output:]
            return f"[...truncated to last {self._max_output} chars]\n{tail}"
        return text

    # -- cleanup ---------------------------------------------------------------

    def __del__(self) -> None:  # best-effort
        proc = getattr(self, "_proc", None)
        if proc is None or proc.returncode is not None:
            return
        try:
            pid = proc.pid
        except Exception:
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
