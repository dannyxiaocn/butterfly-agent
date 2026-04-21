"""Terminal session-state + append-only log for the web Terminal panel.

Each session's persistent shell writes two files under
``sessions/<id>/core/terminal/``:

- ``state.json``: liveness snapshot — ``active`` / ``cwd`` /
  ``last_active_at`` / ``foreground_pid`` / ``foreground_cmd`` /
  ``locked_by``. Rewritten in place on every chunk; reflects the NOW.
- ``log.jsonl``: append-only stream of ``{ts, source, text}`` entries. The
  ``source`` tag is one of ``agent_cmd`` / ``agent_out`` / ``user_cmd`` /
  ``user_out`` / ``system``; the web panel replays this to render the
  terminal history.

``TerminalLogger`` is the single writer. PtyShell pushes read chunks via
``on_chunk`` → ``append_output``; the executor stamps command lines via
``append_command`` and the lock state via ``mark_locked``. File writes are
short (≤ a few KB) and O(1); the append-only jsonl is safe to tail from
the HTTP reader side without coordination.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable


_DEFAULT_STATE: dict[str, Any] = {
    "active": False,
    "cwd": None,
    "last_active_at": None,
    "foreground_pid": None,
    "foreground_cmd": None,
    "locked_by": None,  # None | "agent" | "user"
    "shell_pid": None,
}


class TerminalLogger:
    """Writer for one session's terminal dir. Thread-/coro-safe via lock."""

    def __init__(
        self,
        terminal_dir: Path,
        event_sink: Callable[[dict], None] | None = None,
    ) -> None:
        self._dir = Path(terminal_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self._dir / "log.jsonl"
        self._state_path = self._dir / "state.json"
        self._lock = threading.Lock()
        # Optional: mirror every log/state change onto the SSE event bus so
        # the web frontend gets live updates without tailing log.jsonl.
        self._event_sink = event_sink
        # Ensure files exist so consumers can open them unconditionally.
        if not self._log_path.exists():
            self._log_path.touch()
        if not self._state_path.exists():
            self._write_state_locked(dict(_DEFAULT_STATE))
        # Monotonic sequence id stamped on every log entry — makes log.jsonl
        # the single source of truth. SSE events carry the same seq so the
        # frontend can (a) dedupe when a `GET /terminal` snapshot races
        # with in-flight SSE, and (b) order events reliably without
        # relying on timestamps. On startup we seed seq from the existing
        # line count so a restarted daemon continues numbering.
        self._seq: int = self._count_existing_lines()

    def _count_existing_lines(self) -> int:
        try:
            n = 0
            with self._log_path.open("rb") as f:
                for _ in f:
                    n += 1
            return n
        except OSError:
            return 0

    # ── path accessors ─────────────────────────────────────────────────

    @property
    def log_path(self) -> Path:
        return self._log_path

    @property
    def state_path(self) -> Path:
        return self._state_path

    @property
    def directory(self) -> Path:
        return self._dir

    # ── log writes ─────────────────────────────────────────────────────

    def append_output(self, source: str, text: str) -> None:
        """Called by PtyShell.on_chunk for every raw chunk read from the pty.

        `source` is ``"agent_out"`` or ``"user_out"`` — whichever side
        drove the most recent write (PtyShell tracks this via
        ``_current_source``).
        """
        if not text:
            return
        self._append_entry({"ts": time.time(), "source": source, "text": text})

    def append_command(self, source: str, text: str) -> None:
        """Stamp a command line (pre-write). Shell echo is disabled, so
        the command wouldn't show up via `on_chunk`; the executor must
        log it explicitly for the panel to render "$ <cmd>"."""
        self._append_entry({"ts": time.time(), "source": source, "text": text})

    def append_system(self, text: str) -> None:
        """System-generated notice (shell restarted / snapshot restored / etc.)."""
        self._append_entry({"ts": time.time(), "source": "system", "text": text})

    def _append_entry(self, entry: dict) -> None:
        """Stamp `seq`, write to log.jsonl, emit matching SSE event.

        The lock protects both the seq increment and the file write so a
        concurrent call can't interleave seq assignment vs disk order.
        Emit happens outside the lock because the sink hits events.jsonl
        which has its own lock and we don't want to nest.
        """
        with self._lock:
            entry["seq"] = self._seq
            self._seq += 1
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(line)
        self._patch_state({"last_active_at": entry["ts"]})
        self._emit({"type": "terminal_log", **entry})

    # ── state.json ─────────────────────────────────────────────────────

    def read_state(self) -> dict:
        with self._lock:
            return self._read_state_locked()

    def _read_state_locked(self) -> dict:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return dict(_DEFAULT_STATE)
            merged = dict(_DEFAULT_STATE)
            merged.update(data)
            return merged
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return dict(_DEFAULT_STATE)

    def _write_state_locked(self, state: dict) -> None:
        tmp = self._state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._state_path)

    def _patch_state(self, patch: dict) -> None:
        with self._lock:
            state = self._read_state_locked()
            state.update(patch)
            self._write_state_locked(state)
        self._emit({"type": "terminal_state", "state": state})

    def _emit(self, event: dict) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(event)
        except Exception:
            pass

    def mark_active(self, active: bool, shell_pid: int | None = None) -> None:
        patch: dict[str, Any] = {"active": active}
        if shell_pid is not None:
            patch["shell_pid"] = shell_pid
        patch["last_active_at"] = time.time()
        self._patch_state(patch)

    def mark_locked(self, by: str | None) -> None:
        """`by` ∈ {None, "agent", "user"}."""
        self._patch_state({"locked_by": by, "last_active_at": time.time()})

    def update_foreground(self, pid: int | None, cmd: str | None) -> None:
        self._patch_state({"foreground_pid": pid, "foreground_cmd": cmd})

    def update_cwd(self, cwd: str | None) -> None:
        self._patch_state({"cwd": cwd})
