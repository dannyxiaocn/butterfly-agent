"""Persistent per-session pty terminal (pure_context series).

Replaces the v2.0.33 ``session_shell`` tool — that shape fused
"create pty" and "run command" into one verb, which turned a stale /
freshly-re-created shell into silent-failure ambiguity (observed: first
call with ``reset=true`` returned ``[shell reset]\\n[exit 0]`` and swallowed
the actual ``pwd`` command). The new surface is an explicit two-step:

* ``terminal_create`` — spawn a fresh pty, snapshot
  ``(venv, cwd, git_branch)`` once, return a welcome block.
* ``terminal_use`` — run one command against the already-created
  terminal; errors fail-closed when ``terminal_create`` hasn't run. On
  any env-fingerprint change (the agent cd'd, activated a venv,
  checked out a different branch) we prepend ``[env: … | path: … |
  git: …]`` to the tool result so the model notices the drift without
  re-probing.

Under the hood a long-lived ``bash --norc --noprofile`` is attached to
a pseudo-tty, so interactive commands work: ``ssh host`` stays alive
across calls, ``python`` drops the caller into a REPL, and ``cd`` /
``export`` / aliases persist between calls.

Unified return rule
-------------------
One rule handles every command shape:

1. **Sentinel matches** — we appended ``; printf "\\n__<marker>_%d__\\n" $?``
   to the payload. A match means bash returned to the prompt → emit exit
   code + duration.
2. **Output idle** — no new bytes for ``idle_threshold`` seconds (default
   1.5). Covers ssh-prompt-reached, REPL-waiting-for-input, ``read -p``
   parked on stdin. We return the collected output; the shell stays alive
   and a later call continues talking to whatever subprocess owns the tty.
3. **Total timeout** — hard ceiling (default 60s). Cut-off; collected
   output is returned and marked truncated, but the shell is NOT killed
   (a background compile shouldn't lose its pty).

``shell_state.foreground_cmd`` is a *hint only* — it tells the model
"you're inside an ssh subprocess now" via ``tcgetpgrp(master)``; it does
not influence return timing.

On-chunk hook
-------------
Every byte read from the pty is forwarded to an optional
``on_chunk(source, text)`` callback where ``source`` is ``"agent_out"``
or ``"user_out"`` depending on whose command is in flight.
``TerminalLogger.append_output`` is the production wire-up — it feeds
the web panel's ``log.jsonl`` stream.
"""
from __future__ import annotations

import asyncio
import json
import os
import pty
import re
import secrets
import shlex
import signal
import struct
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from butterfly.core.guardian import Guardian
from butterfly.tool_engine.executor.pure_context.base import strip_ansi

_MAX_OUTPUT = 10_000
_BUFFER_FACTOR = 2
_IDLE_THRESHOLD = 1.5
_DEFAULT_TIMEOUT = 60.0
_READ_CHUNK = 65536
# Phase 5: if the shell is idle this long, snapshot its cwd and hard-kill
# the process; the next call (agent or user) restores cwd on respawn.
_IDLE_CLOSE_SECONDS = 600.0
# Snapshots older than this are ignored on restore (stale; env has drifted).
_SNAPSHOT_MAX_AGE = 7 * 24 * 3600.0
_SNAPSHOT_FILENAME = "snapshot.json"

# Exact string surfaced by ``terminal_use`` when called before
# ``terminal_create``. The agent-facing phrasing is part of the tool's
# public contract; tests pin it verbatim.
_NOT_CREATED_ERROR = (
    "Error: Terminal not created, please use terminal_create tool to "
    "create one first"
)

OnChunk = Callable[[str, str], None]
OnChunkAsync = Callable[[str, str], Awaitable[None]]


def _homify(path: str | None) -> str | None:
    """Render an absolute path with ``$HOME`` replaced by ``~``.

    Matches the zsh/bash ``%~`` prompt expansion so the web panel's
    HUD row looks like a normal terminal prompt (``(base) ~/work: main``
    instead of ``(base) /Users/<username>/work: main``). Falls through
    untouched when the path doesn't start with home, when ``HOME`` is
    unset (rare — tests), or when the caller passed ``None``.
    """
    if not path:
        return path
    home = os.environ.get("HOME") or os.path.expanduser("~")
    if not home or home == "~":
        return path
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


def _try_import_fcntl():
    try:
        import fcntl
        import termios
        return fcntl, termios
    except Exception:
        return None, None


# Any chunk matching this pattern is a synchronization artifact from the
# sentinel protocol — we strip it before forwarding to the Terminal-panel
# log so users don't see `__BFY_DONE_abc123_0__` in their output. The tool
# return value has the same text trimmed via `run_command`'s scanner; this
# filter is only for the separate `on_chunk` listener path.
_SENTINEL_LEAK = re.compile(rb"\n?__BFY_DONE_[0-9a-f]+_-?\d+__\n?")

# Env-probe markers: each detect_env() run boxes the 3 lines of interest
# with unique markers so we can slice them out of the noisy pty output
# (bash's own echo, any trailing newline games). Markers are regenerated
# per probe — random hex keeps them unique even under concurrent calls.


@dataclass
class RunResult:
    output: str            # ANSI-stripped text, capped to max_output
    exit_code: int | None  # None when sentinel didn't fire
    duration: float        # total wall time, incl. any interrupt recovery
    timeout: float         # the threshold this run was configured with
    idle_threshold: float  # the idle-settle threshold this run was configured with
    idle_return: bool      # True when we returned on idle, not sentinel
    timed_out: bool        # True on total-timeout cut-off
    foreground_cmd: str | None  # hint: who owns the pty now
    foreground_pid: int | None
    shell_alive: bool
    restarted: bool        # shell was respawned just before this run


@dataclass
class EnvFingerprint:
    """Snapshot of ``(venv, cwd, git_branch, git_dirty)`` captured inside
    the pty. ``git_dirty`` is ``None`` when we're not inside a git repo
    (or the probe fails), ``True`` when ``git status --porcelain`` had
    any output, ``False`` otherwise. The HUD renders it as a trailing
    ``*`` on the branch name."""
    venv: str | None
    cwd: str | None
    git_branch: str | None
    git_dirty: bool | None = None

    def as_tuple(self) -> tuple[str | None, str | None, str | None, bool | None]:
        return (self.venv, self.cwd, self.git_branch, self.git_dirty)

    def as_dict(self) -> dict[str, Any]:
        return {
            "venv": self.venv,
            "cwd": self.cwd,
            "git_branch": self.git_branch,
            "git_dirty": self.git_dirty,
        }


class PtyShell:
    """Persistent bash on a pty. One event-loop owner; serialize at the executor."""

    def __init__(
        self,
        workdir: str | None,
        env_provider: Callable[[], dict[str, str]],
        on_chunk: OnChunk | None = None,
        max_output: int = _MAX_OUTPUT,
    ) -> None:
        self._workdir = workdir
        self._env_provider = env_provider
        self._on_chunk = on_chunk
        self._max_output = max_output
        self._master: int | None = None
        self._proc: subprocess.Popen | None = None
        self._buffer = bytearray()
        self._eof = False
        self._readable = asyncio.Event()
        self._current_source = "agent_out"  # "agent_out" or "user_out"
        self._ever_spawned = False
        self._last_activity: float = 0.0  # monotonic; 0 == never active
        # Suppress on_chunk forwarding while we swallow bash's startup
        # banner (macOS zsh-deprecation notice, our init probe echo, any
        # /etc/bashrc-ish hook output). Flipped to False once we've seen
        # the spawn-marker sentinel or the drain deadline elapses.
        self._draining: bool = False

    # ── lifecycle ──────────────────────────────────────────────────────────

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def ever_spawned(self) -> bool:
        return self._ever_spawned

    def last_activity(self) -> float:
        """Monotonic timestamp of the last I/O activity (0 if never)."""
        return self._last_activity

    def _bump_activity(self) -> None:
        self._last_activity = time.monotonic()

    async def spawn(self) -> None:
        """Start a fresh bash on a fresh pty. Drain startup chatter.

        Pre-configures the pty termios on the slave side (disables ECHO +
        ONLCR) *before* forking bash, so bash never echoes our init probe
        back into the terminal log. Then sends a spawn marker and
        consumes everything up to and including it — on macOS that covers
        the zsh-deprecation notice some system-wide hook loves to print
        even against ``--norc --noprofile``.
        """
        env = self._env_provider()
        master, slave = pty.openpty()
        os.set_blocking(master, False)

        fcntl, termios = _try_import_fcntl()
        if termios is not None:
            # Turn off echo + LF→CRLF translation before bash takes over
            # the slave fd. ISIG stays on so ^C via the pty still works.
            try:
                attrs = termios.tcgetattr(slave)
                # index 1 = oflag, 3 = lflag  (per termios spec)
                attrs[1] &= ~termios.ONLCR
                attrs[3] &= ~(termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL)
                termios.tcsetattr(slave, termios.TCSANOW, attrs)
            except (OSError, Exception):
                pass
        if fcntl is not None and termios is not None:
            try:
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            except Exception:
                pass
        try:
            proc = subprocess.Popen(
                ["bash", "--norc", "--noprofile"],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=self._workdir,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            os.close(slave)

        self._master = master
        self._proc = proc
        self._buffer.clear()
        self._eof = False
        self._readable.clear()
        self._ever_spawned = True
        self._draining = True  # suppress on_chunk until the marker lands

        loop = asyncio.get_event_loop()
        loop.add_reader(master, self._on_readable)

        # Blank the prompt, shut down a few interactive niceties, then emit
        # a spawn marker we can scan for. Everything before the marker is
        # bash's startup noise + potential OS hook output — discarded.
        spawn_marker = f"__BFY_SPAWN_{secrets.token_hex(4)}__"
        init = (
            "PS1=''; PS2=''; "
            "set +o emacs 2>/dev/null; set +o vi 2>/dev/null; "
            "unset PROMPT_COMMAND; "
            f"printf '{spawn_marker}\\n'\n"
        ).encode()
        try:
            os.write(master, init)
        except OSError:
            # Init-write failed (e.g. bash died instantly). ``hard_kill``
            # does the full cleanup — remove_reader + close(master) +
            # SIGTERM→SIGKILL the child — so we don't leak the fd, the
            # asyncio reader callback, or the child process. Re-raise so
            # the caller can surface the spawn failure instead of ending
            # up with a half-alive PtyShell.
            self._draining = False
            await self.hard_kill()
            raise

        marker_bytes = spawn_marker.encode()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if marker_bytes in self._buffer:
                break
            try:
                await asyncio.wait_for(self._readable.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            self._readable.clear()
        # Discard everything up to and including the marker line so the
        # first real output the user sees comes from their own command.
        idx = self._buffer.find(marker_bytes)
        if idx >= 0:
            # Also strip the trailing newline bash appends to printf.
            end = idx + len(marker_bytes)
            if end < len(self._buffer) and self._buffer[end:end + 1] == b"\n":
                end += 1
            del self._buffer[:end]
        else:
            # Marker didn't arrive in time — drop everything; agent just
            # starts cold. Rare path; better than polluting the log.
            self._buffer.clear()
        self._draining = False

    async def hard_kill(self) -> None:
        """SIGTERM → 500ms grace → SIGKILL. Close the pty."""
        if self._proc is None:
            self._close_master()
            return
        if self._proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    self._proc.terminate()
                except ProcessLookupError:
                    pass
            # Wait up to 500ms for exit
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and self._proc.poll() is None:
                await asyncio.sleep(0.02)
            if self._proc.poll() is None:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        self._proc.kill()
                    except ProcessLookupError:
                        pass
                try:
                    self._proc.wait(timeout=0.5)
                except Exception:
                    pass
        self._proc = None
        self._close_master()

    def _close_master(self) -> None:
        if self._master is None:
            return
        try:
            loop = asyncio.get_event_loop()
            loop.remove_reader(self._master)
        except Exception:
            pass
        try:
            os.close(self._master)
        except OSError:
            pass
        self._master = None

    # ── async I/O ─────────────────────────────────────────────────────────

    def _on_readable(self) -> None:
        if self._master is None:
            return
        try:
            chunk = os.read(self._master, _READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            self._eof = True
            self._readable.set()
            return
        if not chunk:
            self._eof = True
            self._readable.set()
            return
        self._buffer.extend(chunk)
        self._bump_activity()
        # Forward to listener (terminal log). Suppressed during spawn
        # drain so startup chatter doesn't reach the web panel. Sentinel
        # artifacts (__BFY_DONE_xxx__) are scrubbed because they're a bash
        # synchronization mechanism, not something either the agent or
        # the user typed.
        if not self._draining and self._on_chunk is not None:
            display_bytes = _SENTINEL_LEAK.sub(b"", chunk)
            if display_bytes:
                try:
                    text = display_bytes.decode(errors="replace")
                    if text:
                        self._on_chunk(self._current_source, text)
                except Exception:
                    pass
        self._readable.set()

    async def _read_any(self, timeout: float | None) -> bytes:
        """Return whatever is buffered; if nothing, wait up to `timeout`.

        Empty-bytes return means 'idle deadline hit'. EOF is surfaced via
        `self._eof` — caller should check it after a zero-byte return.
        """
        if self._buffer:
            chunk = bytes(self._buffer)
            self._buffer.clear()
            return chunk
        if self._eof:
            return b""
        self._readable.clear()
        if self._buffer:
            chunk = bytes(self._buffer)
            self._buffer.clear()
            return chunk
        try:
            await asyncio.wait_for(self._readable.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return b""
        chunk = bytes(self._buffer)
        self._buffer.clear()
        return chunk

    async def write_raw(self, data: bytes, source: str = "agent_out") -> None:
        """Write arbitrary bytes to the pty master. `source` tags the next
        incoming chunks for the on_chunk listener."""
        if self._master is None:
            raise RuntimeError("shell not alive")
        self._current_source = source
        os.write(self._master, data)
        self._bump_activity()

    async def send_interrupt(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGINT)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    async def collect_until_idle(
        self,
        *,
        timeout: float = 30.0,
        idle: float = 1.5,
    ) -> str:
        """Read pty output until it idles for `idle`s or total `timeout`.

        Used by the user-input path where no sentinel is injected — we
        just need to capture whatever the shell produced for this command
        so the panel can report it to the agent. Returns ANSI-stripped
        text, capped to `max_output`.
        """
        start = time.monotonic()
        last = start
        buf = bytearray()
        while True:
            now = time.monotonic()
            tot = timeout - (now - start)
            idl = idle - (now - last)
            if tot <= 0 or idl <= 0:
                break
            chunk = await self._read_any(min(tot, idl))
            if not chunk:
                if self._eof:
                    break
                # Either total- or idle-timeout — exit either way.
                break
            buf.extend(chunk)
            last = time.monotonic()
        cleaned = strip_ansi(buf.decode(errors="replace")).rstrip()
        return _cap(cleaned, self._max_output)

    # ── introspection ─────────────────────────────────────────────────────

    def foreground_info(self) -> tuple[str | None, int | None]:
        """Return (cmd, pid) of the pty's current foreground process group
        leader — a hint about who owns stdin right now. Best-effort; any
        error returns (None, None)."""
        if self._master is None or self._proc is None:
            return None, None
        try:
            pgid = os.tcgetpgrp(self._master)
        except OSError:
            return None, None
        if pgid <= 0:
            return None, None
        # Resolve pgid → a representative pid → its comm
        try:
            cmd = _read_proc_comm(pgid)
        except Exception:
            cmd = None
        return cmd, pgid

    # ── one command ───────────────────────────────────────────────────────

    async def run_command(
        self,
        command: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        idle_threshold: float = _IDLE_THRESHOLD,
    ) -> RunResult:
        """Send `command`, collect output, return under the 3-condition rule."""
        if not self.is_alive():
            raise RuntimeError("shell not alive")

        marker = f"BFY_DONE_{secrets.token_hex(4)}"
        pattern = re.compile(rf"__{re.escape(marker)}_(-?\d+)__")

        # `;` (not `&&`) so the sentinel prints even if `command` fails.
        # Multi-line `command` is fine — bash executes line by line.
        payload = (command + "\n" + f'printf "\\n__{marker}_%d__\\n" $?\n').encode()
        try:
            await self.write_raw(payload, source="agent_out")
        except OSError:
            return RuntimeError_to_result("shell write failed", restarted=False)

        start = time.monotonic()
        last_activity = start
        collected: deque[bytes] = deque()
        collected_len = 0
        buffer_cap = max(self._max_output * _BUFFER_FACTOR, self._max_output)

        def _append(data: bytes) -> None:
            nonlocal collected_len
            collected.append(data)
            collected_len += len(data)
            while collected_len > buffer_cap and collected:
                popped = collected.popleft()
                collected_len -= len(popped)

        def _scan_sentinel() -> int | None:
            """Scan the whole collected buffer for the sentinel; if found,
            trim collected to pre-sentinel text and return the exit code."""
            nonlocal collected_len
            joined = b"".join(collected).decode(errors="replace")
            m = pattern.search(joined)
            if not m:
                return None
            before = joined[: m.start()].rstrip("\n")
            collected.clear()
            collected_len = 0
            _append(before.encode(errors="replace"))
            return int(m.group(1))

        exit_code: int | None = None
        idle_return = False
        timed_out = False
        shell_died = False

        while True:
            now = time.monotonic()
            total_left = timeout - (now - start)
            idle_left = idle_threshold - (now - last_activity)
            if total_left <= 0:
                timed_out = True
                break
            if idle_left <= 0:
                idle_return = True
                break
            wait_for = min(total_left, idle_left)
            chunk = await self._read_any(wait_for)
            if not chunk:
                if self._eof:
                    shell_died = True
                    break
                now2 = time.monotonic()
                if now2 - start >= timeout:
                    timed_out = True
                else:
                    idle_return = True
                break
            last_activity = time.monotonic()
            _append(chunk)
            rc = _scan_sentinel()
            if rc is not None:
                exit_code = rc
                break

        # Total-timeout recovery: pass ^C through the pty (reaches the
        # foreground process group, including across ssh). Wait briefly for
        # bash to print the sentinel. On success we keep the shell; on
        # failure we don't kill it either — the agent can call
        # terminal_create again if needed.
        if timed_out and not shell_died:
            try:
                await self.write_raw(b"\x03", source="agent_out")
            except OSError:
                pass
            recovery_deadline = time.monotonic() + 2.0
            while time.monotonic() < recovery_deadline:
                left = recovery_deadline - time.monotonic()
                chunk = await self._read_any(left)
                if not chunk:
                    if self._eof:
                        shell_died = True
                    break
                _append(chunk)
                rc = _scan_sentinel()
                if rc is not None:
                    exit_code = rc
                    break

        duration = time.monotonic() - start
        raw = b"".join(collected).decode(errors="replace")
        cleaned = strip_ansi(raw).rstrip()
        output = _cap(cleaned, self._max_output)

        fg_cmd, fg_pid = (None, None)
        if self.is_alive():
            fg_cmd, fg_pid = self.foreground_info()

        if shell_died:
            self._close_master()

        return RunResult(
            output=output,
            exit_code=exit_code,
            duration=duration,
            timeout=timeout,
            idle_threshold=idle_threshold,
            idle_return=idle_return and not timed_out and not shell_died,
            timed_out=timed_out,
            foreground_cmd=fg_cmd,
            foreground_pid=fg_pid,
            shell_alive=self.is_alive(),
            restarted=False,
        )


def _read_proc_comm(pid: int) -> str | None:
    """Best-effort lookup of a pid's comm (process name)."""
    # Linux: /proc/<pid>/comm
    try:
        with open(f"/proc/{pid}/comm", "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        pass
    except OSError:
        return None
    # Darwin/BSD fallback: `ps -p <pid> -o comm=`
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "comm="],
            stderr=subprocess.DEVNULL,
            timeout=0.5,
        )
        # ps returns basename; strip path
        name = out.decode(errors="replace").strip().splitlines()
        if not name:
            return None
        return os.path.basename(name[-1]) or None
    except Exception:
        return None


def _cap(text: str, max_output: int) -> str:
    if len(text) > max_output:
        tail = text[-max_output:]
        return f"[...truncated to last {max_output} chars]\n{tail}"
    return text


def RuntimeError_to_result(msg: str, *, restarted: bool) -> RunResult:
    return RunResult(
        output=f"[{msg}]",
        exit_code=None,
        duration=0.0,
        timeout=0.0,
        idle_threshold=_IDLE_THRESHOLD,
        idle_return=False,
        timed_out=False,
        foreground_cmd=None,
        foreground_pid=None,
        shell_alive=False,
        restarted=restarted,
    )


# ─────────────────────────────────────────────────────────────────────────
# Executor — backs both terminal_create and terminal_use
# ─────────────────────────────────────────────────────────────────────────


class TerminalExecutor:
    """Domain class driving the persistent pty.

    ``terminal_create`` and ``terminal_use`` are two toolhub tools that
    share one ``TerminalExecutor`` instance per session (Session owns it;
    the tool loader injects the same object for both). The class is
    intentionally NOT a ``BaseExecutor`` — the two verbs have different
    return shapes, and the ``BaseExecutor.execute`` single-entry-point
    contract encouraged the old ``reset``-param footgun.

    Public verbs (both async):

    * ``create()`` → ``str``: ensures the pty is alive, snapshots the
      initial env, returns a 4-line welcome block.
    * ``use(command, timeout?, idle_threshold?)`` → ``str``: errors when
      ``create()`` hasn't run; otherwise runs one command and prepends a
      ``[env: …]`` line when the venv / cwd / git-branch fingerprint
      changed since the last observation.

    The web-panel user-input path (``user_input``, ``user_interrupt``)
    and the idle-close housekeeping (``snapshot_and_close``,
    ``maybe_idle_close``) stay on this class — they operate on the same
    pty.
    """

    def __init__(
        self,
        workdir: str | None = None,
        venv_env_provider: Optional[Callable[[], Optional[dict[str, str]]]] = None,
        max_output: int = _MAX_OUTPUT,
        guardian: Guardian | None = None,
        on_chunk: OnChunk | None = None,
        terminal_logger: Any | None = None,  # butterfly.session_engine.terminal.TerminalLogger
    ) -> None:
        self._guardian = guardian
        if guardian is not None:
            workdir = str(guardian.root)
        self._workdir = workdir
        self._venv_env_provider = venv_env_provider
        self._max_output = max_output
        self._lock = asyncio.Lock()
        self._locked_by_agent = False
        self._terminal_logger = terminal_logger

        # "Created" = the agent has explicitly opened this terminal via
        # ``terminal_create``. ``use`` fails fast when false. The flag
        # persists as long as the executor lives — a snapshot-and-close
        # from the idle path leaves it True so the next ``use`` can
        # transparently re-spawn (the shell is gone; the *terminal
        # session* the agent knows about is not).
        self._is_created: bool = False
        self._last_env: EnvFingerprint | None = None

        # If a TerminalLogger was provided, route all pty chunks through it.
        # An explicit on_chunk still wins for callers that want their own
        # stream (tests) — the logger's hook fires only when on_chunk is
        # None.
        effective_on_chunk = on_chunk
        if effective_on_chunk is None and terminal_logger is not None:
            effective_on_chunk = terminal_logger.append_output

        self._shell = PtyShell(
            workdir=workdir,
            env_provider=self._build_env,
            on_chunk=effective_on_chunk,
            max_output=max_output,
        )

    @property
    def shell(self) -> PtyShell:
        return self._shell

    @property
    def locked_by_agent(self) -> bool:
        return self._locked_by_agent

    @property
    def is_created(self) -> bool:
        return self._is_created

    @property
    def last_env(self) -> EnvFingerprint | None:
        return self._last_env

    def _build_env(self) -> dict[str, str]:
        env: dict[str, str] | None = None
        if self._venv_env_provider is not None:
            try:
                env = self._venv_env_provider()
            except Exception:
                env = None
        if env is None:
            env = os.environ.copy()
        env["HISTFILE"] = ""
        env.setdefault("TERM", "xterm-256color")
        if self._guardian is not None:
            env["BUTTERFLY_GUARDIAN_ROOT"] = str(self._guardian.root)
        return env

    async def _ensure_alive(self) -> bool:
        """Spawn if needed. Return True only if this call is a *restart*
        (i.e. a prior spawn existed and is now gone)."""
        if self._shell.is_alive():
            return False
        was_restart = self._shell.ever_spawned()
        await self._shell.spawn()
        if self._terminal_logger is not None:
            pid = self._shell._proc.pid if self._shell._proc is not None else None
            self._terminal_logger.mark_active(True, shell_pid=pid)
            if was_restart:
                self._terminal_logger.append_system("[shell restarted]")
            else:
                self._terminal_logger.append_system("[shell opened]")
        # Phase 5: if a fresh snapshot is on disk, restore cwd before the
        # first command runs.
        await self._maybe_restore()
        return was_restart

    async def close(self) -> None:
        """Tear down the underlying shell. Safe to call repeatedly."""
        await self._shell.hard_kill()
        if self._terminal_logger is not None:
            self._terminal_logger.mark_active(False)

    # ── Env fingerprint ──────────────────────────────────────────────

    async def _detect_env(self) -> EnvFingerprint:
        """Probe ``(venv, cwd, git_branch, git_dirty)`` from inside the pty.

        Emits one 4-line batch boxed by unique start/end markers so we
        can slice the values even when bash's own output or prior
        chatter sits in the buffer. Best-effort: any probe failure
        degrades to ``None`` for that field rather than aborting.

        The ``git_dirty`` line is one of ``clean`` / ``dirty`` / empty.
        Empty (or missing) means we couldn't determine the state — we
        treat that as ``None`` at the type level and the HUD won't
        show the ``*`` modifier.
        """
        null_fp = EnvFingerprint(
            venv=None, cwd=None, git_branch=None, git_dirty=None
        )
        if not self._shell.is_alive():
            return null_fp
        tag = secrets.token_hex(4)
        start = f"__BFY_ENV_START_{tag}__"
        end = f"__BFY_ENV_END_{tag}__"
        cmd = (
            f'printf "{start}\\n"; '
            'printf "%s\\n" "${CONDA_DEFAULT_ENV:-${VIRTUAL_ENV##*/}}"; '
            'pwd; '
            "git rev-parse --abbrev-ref HEAD 2>/dev/null || printf '\\n'; "
            '[ -n "$(git status --porcelain 2>/dev/null)" ] '
            "&& printf 'dirty\\n' || printf 'clean\\n'; "
            f'printf "{end}\\n"'
        )
        try:
            result = await self._shell.run_command(
                cmd, timeout=3.0, idle_threshold=0.4
            )
        except RuntimeError:
            return null_fp
        text = result.output or ""
        s_idx = text.find(start)
        e_idx = text.find(end, s_idx + 1 if s_idx >= 0 else 0)
        if s_idx < 0 or e_idx < 0:
            return null_fp
        # `printf "{start}\\n"` emits a newline immediately after the
        # marker; skip that one specifically so an empty leading field
        # (e.g. no venv set) doesn't get stripped and slide every
        # subsequent field up by one.
        body = text[s_idx + len(start):e_idx]
        if body.startswith("\n"):
            body = body[1:]
        lines = body.splitlines()
        venv = lines[0].strip() if len(lines) >= 1 else ""
        cwd = lines[1].strip() if len(lines) >= 2 else ""
        git = lines[2].strip() if len(lines) >= 3 else ""
        dirty_raw = lines[3].strip() if len(lines) >= 4 else ""
        # ``git_dirty`` is only meaningful when we're in a repo. Pin it
        # to None when no branch so the HUD doesn't hang a stray ``*``
        # off a null path.
        if not git:
            dirty: bool | None = None
        elif dirty_raw == "dirty":
            dirty = True
        elif dirty_raw == "clean":
            dirty = False
        else:
            dirty = None
        return EnvFingerprint(
            venv=venv or None,
            cwd=cwd or None,
            git_branch=git or None,
            git_dirty=dirty,
        )

    def _publish_env(self, env: EnvFingerprint) -> None:
        """Mirror the fingerprint to ``state.json`` so the panel HUD row
        reflects the current ``env / path / git`` without a separate
        probe. Silent when the logger isn't wired (tests).

        Publishes both ``cwd`` (raw absolute path, used by snapshot+restore)
        and ``cwd_display`` (``~``-shortened form the HUD renders).
        Writes all fields in one patch so the frontend sees one
        ``terminal_state`` SSE per fingerprint change, not four.
        """
        self._last_env = env
        if self._terminal_logger is None:
            return
        self._terminal_logger.update_fingerprint(
            venv=env.venv,
            cwd=env.cwd,
            cwd_display=_homify(env.cwd),
            git_branch=env.git_branch,
            git_dirty=env.git_dirty,
        )

    # ── Welcome / env-change formatting ──────────────────────────────

    @staticmethod
    def _format_welcome(env: EnvFingerprint) -> str:
        """4-line welcome block returned by ``terminal_create``."""
        return (
            "[terminal ready]\n"
            f"env: {env.venv or 'null'}\n"
            f"path: {env.cwd or 'null'}\n"
            f"git: {env.git_branch or 'null'}"
        )

    @staticmethod
    def _format_env_change(env: EnvFingerprint) -> str:
        """One-line banner prepended when the env fingerprint changed
        between two ``terminal_use`` calls."""
        return (
            f"[env: {env.venv or 'null'} | "
            f"path: {env.cwd or 'null'} | "
            f"git: {env.git_branch or 'null'}]"
        )

    # ── terminal_create ──────────────────────────────────────────────

    async def create(self, **_kwargs: Any) -> str:
        """Ensure the pty is alive and return the welcome block.

        Idempotent: calling twice is a no-op on the process (the existing
        shell stays alive) and just re-reports the current fingerprint.
        If the process died in between, we respawn silently.
        """
        async with self._lock:
            self._locked_by_agent = True
            if self._terminal_logger is not None:
                self._terminal_logger.mark_locked("agent")
            try:
                await self._ensure_alive()
                env = await self._detect_env()
                self._is_created = True
                self._publish_env(env)
                return self._format_welcome(env)
            finally:
                self._locked_by_agent = False
                if self._terminal_logger is not None:
                    self._terminal_logger.mark_locked(None)

    # ── terminal_use ─────────────────────────────────────────────────

    async def use(self, **kwargs: Any) -> str:
        """Run one command against the already-created terminal."""
        if not self._is_created:
            return _NOT_CREATED_ERROR

        command = kwargs.get("command")
        if not isinstance(command, str):
            return "Error: `command` (string) is required.\n[exit unknown]"
        raw_timeout = kwargs.get("timeout")
        if raw_timeout is None:
            timeout = _DEFAULT_TIMEOUT
        else:
            try:
                timeout = float(raw_timeout)
            except (TypeError, ValueError):
                timeout = _DEFAULT_TIMEOUT
        raw_idle = kwargs.get("idle_threshold")
        if raw_idle is None:
            idle_threshold = _IDLE_THRESHOLD
        else:
            try:
                idle_threshold = float(raw_idle)
            except (TypeError, ValueError):
                idle_threshold = _IDLE_THRESHOLD

        async with self._lock:
            self._locked_by_agent = True
            if self._terminal_logger is not None:
                self._terminal_logger.mark_locked("agent")
            try:
                was_restart = await self._ensure_alive()
                prefix = "[shell restarted]\n" if was_restart else ""

                # Stamp the command for panel replay BEFORE write so it
                # precedes the output chunks in log ordering.
                if self._terminal_logger is not None:
                    self._terminal_logger.append_command("agent_cmd", command)

                try:
                    result = await self._shell.run_command(
                        command, timeout=timeout, idle_threshold=idle_threshold
                    )
                except RuntimeError as e:
                    return f"{prefix}[{e}]\n[exit unknown]"

                if self._terminal_logger is not None:
                    self._terminal_logger.update_foreground(
                        result.foreground_pid, result.foreground_cmd
                    )
                    if not result.shell_alive:
                        self._terminal_logger.mark_active(False)
                        self._terminal_logger.append_system("[shell died]")

                # Re-probe env; if changed (or we have no prior), prepend
                # the banner so the agent notices without extra round-trips.
                # Only probe when bash returned to its prompt (sentinel
                # matched). If we returned on idle or total-timeout, some
                # subprocess — ``python3``, ``ssh``, ``read -p`` — still
                # owns the tty; sending ``pwd; git rev-parse`` at it would
                # hit the REPL (SyntaxError), round-trip no markers,
                # produce ``null_fp``, and then blank the HUD + prepend
                # ``[env: null | path: null | git: null]``.
                env_banner = ""
                if result.shell_alive and not result.idle_return and not result.timed_out:
                    new_env = await self._detect_env()
                    if self._last_env is None or new_env.as_tuple() != self._last_env.as_tuple():
                        env_banner = self._format_env_change(new_env) + "\n"
                    self._publish_env(new_env)

                return prefix + env_banner + _format_result(result)
            finally:
                self._locked_by_agent = False
                if self._terminal_logger is not None:
                    self._terminal_logger.mark_locked(None)

    # ── Phase 5: idle close + restore ─────────────────────────────────

    def _snapshot_path(self) -> Path | None:
        if self._terminal_logger is None:
            return None
        return self._terminal_logger.directory / _SNAPSHOT_FILENAME

    async def snapshot_and_close(self) -> bool:
        """Capture cwd, write snapshot.json, then hard-kill the shell.

        Returns True if a snapshot was written. Silently skips when the
        shell isn't alive or the logger isn't wired (tests/CLI path).
        """
        snap_path = self._snapshot_path()
        if snap_path is None:
            return False
        if not self._shell.is_alive():
            return False
        async with self._lock:
            if self._locked_by_agent:
                return False
            cwd: str | None = None
            if self._last_env is not None:
                cwd = self._last_env.cwd
            if cwd is None:
                # Fall back to a one-shot `pwd` probe.
                try:
                    result = await self._shell.run_command(
                        "pwd", timeout=2.0, idle_threshold=0.5
                    )
                    for line in (result.output or "").splitlines():
                        s = line.strip()
                        if s.startswith("/"):
                            cwd = s
                            break
                except Exception:
                    cwd = None
            snapshot = {
                "ts": time.time(),
                "cwd": cwd,
            }
            try:
                snap_path.write_text(
                    json.dumps(snapshot, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                pass
            if self._terminal_logger is not None:
                self._terminal_logger.append_system(
                    f"[shell idle > {int(_IDLE_CLOSE_SECONDS)}s — closed; state snapshot saved]"
                )
            await self._shell.hard_kill()
            if self._terminal_logger is not None:
                self._terminal_logger.mark_active(False)
        return True

    async def maybe_idle_close(self, threshold: float = _IDLE_CLOSE_SECONDS) -> bool:
        """Snapshot+close when idle > threshold seconds. No-op otherwise."""
        if not self._shell.is_alive():
            return False
        if self._locked_by_agent:
            return False
        last = self._shell.last_activity()
        if last == 0 or (time.monotonic() - last) < threshold:
            return False
        return await self.snapshot_and_close()

    async def _maybe_restore(self) -> None:
        """Inject `cd <cwd>` + banner on fresh-spawn when a snapshot exists."""
        snap_path = self._snapshot_path()
        if snap_path is None or not snap_path.exists():
            return
        try:
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        age = time.time() - float(snap.get("ts", 0) or 0)
        if age <= 0 or age > _SNAPSHOT_MAX_AGE:
            return
        cwd = snap.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            return
        # Run the cd through ``run_command`` so its output (empty in the
        # happy path, or a harmless blank line) is drained before the
        # agent's real command lands. ``write_raw`` + no drain left the
        # cd's output in the buffer and the next ``run_command`` picked
        # it up as its own header line. ``2>/dev/null`` silences the
        # directory-gone error; ``|| true`` swallows its exit code.
        probe = f"cd {shlex.quote(cwd)} 2>/dev/null || true"
        try:
            await self._shell.run_command(probe, timeout=2.0, idle_threshold=0.3)
        except RuntimeError:
            return
        if self._terminal_logger is not None:
            self._terminal_logger.append_system(
                f"[previous session ended {int(age)}s ago — cwd restored: {cwd}]"
            )

    # ── User-input path (web panel) ──────────────────────────────────

    async def user_input(self, content: str) -> tuple[bool, str]:
        """Write user-typed text to the pty, then collect resulting output.

        Returns ``(accepted, output)``:
          - ``accepted=False`` → agent holds the lock; caller should 409.
          - ``accepted=True`` → text written; ``output`` is the captured
            pty bytes (ANSI-stripped, capped) until output idled for 1.5s
            or total 30s elapsed. Session forwards it to ``context.jsonl``
            so the agent sees the ``$ cmd\\n<output>`` transcript.

        `content` is written verbatim (include your own trailing newline if
        you want the shell to execute it). The text is logged as
        ``user_cmd`` and any output chunks that follow are tagged
        ``user_out`` via PtyShell's `_current_source`.
        """
        if self._locked_by_agent:
            return False, ""
        async with self._lock:
            if self._locked_by_agent:
                return False, ""
            if self._terminal_logger is not None:
                self._terminal_logger.mark_locked("user")
            try:
                await self._ensure_alive()
                if self._terminal_logger is not None:
                    self._terminal_logger.append_command(
                        "user_cmd", content.rstrip("\n") or content
                    )
                await self._shell.write_raw(content.encode(), source="user_out")
                output = await self._shell.collect_until_idle()
                # A user `cd` / `conda activate` / `git switch` updates
                # the fingerprint; refresh so the HUD pill reflects it.
                # Skip the probe when a subprocess still owns the tty
                # (user ran ``python3`` / ``ssh host`` and is parked in
                # the REPL): the ``pwd; git rev-parse`` probe would
                # land in the REPL, round-trip no markers, and blank
                # the HUD. ``foreground_info()`` reports the current
                # pgid's command via ``tcgetpgrp(master)``.
                fg_cmd, _fg_pid = self._shell.foreground_info()
                if fg_cmd is None or fg_cmd in ("bash", "-bash", "zsh", "-zsh", "sh"):
                    try:
                        env = await self._detect_env()
                        self._publish_env(env)
                    except Exception:
                        pass
            finally:
                if self._terminal_logger is not None:
                    self._terminal_logger.mark_locked(None)
        return True, output

    async def user_interrupt(self) -> bool:
        """Send Ctrl-C to the pty. Rejected while agent holds the lock
        (use the global ⚡ Interrupt button for that case)."""
        if self._locked_by_agent:
            return False
        if not self._shell.is_alive():
            return True
        try:
            await self._shell.write_raw(b"\x03", source="user_out")
        except OSError:
            return False
        return True


def _format_result(r: RunResult) -> str:
    """Render a RunResult as the `[…]`-footer string the model sees."""
    body = r.output
    fg_hint = ""
    if r.foreground_cmd:
        fg_hint = f", foreground={r.foreground_cmd}"

    if not r.shell_alive:
        return f"{body}\n[shell died]\n[exit unknown]"
    if r.timed_out:
        if r.exit_code is not None:
            return (
                f"{body}\n[timed out after {_fmt_dur(r.timeout)}s, interrupted, "
                f"exit {r.exit_code}{fg_hint}]"
            )
        return (
            f"{body}\n[timed out after {_fmt_dur(r.timeout)}s, shell still alive "
            f"but may be stuck{fg_hint}; call terminal_create again to reset]"
        )
    if r.exit_code is not None:
        return f"{body}\n[exit {r.exit_code}, duration {_fmt_dur(r.duration)}s{fg_hint}]"
    # Idle return: sentinel didn't fire, shell is parked in a subprocess.
    return (
        f"{body}\n[no exit captured — output idle for {_fmt_dur(r.idle_threshold)}s"
        f"{fg_hint}; shell still alive, send next command to continue]"
    )


def _fmt_dur(d: float) -> str:
    return f"{d:.1f}" if d >= 0.1 else f"{d:.2f}"


__all__ = [
    "EnvFingerprint",
    "PtyShell",
    "RunResult",
    "TerminalExecutor",
]
