"""Web-facing helpers for the per-session Terminal panel.

Write-side endpoints go through ``core/terminal/input.jsonl`` — an
append-only queue the session daemon tails. Read-side endpoints serve
``state.json`` + paged ``log.jsonl``. No BridgeSession involvement
because the contract is entirely file-based.
"""
from __future__ import annotations

import json
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

# Tail size for the initial panel load. Reload-safe: the frontend replays
# these then subscribes to terminal_log SSE events for deltas.
_DEFAULT_TAIL = 500
# Bytes to scan at most when reading the log from an offset.
_MAX_READ_BYTES = 512 * 1024


def _terminal_dir(sessions_dir: Path, session_id: str) -> Path:
    return sessions_dir / session_id / "core" / "terminal"


def _input_queue_path(terminal_dir: Path) -> Path:
    return terminal_dir / "input.jsonl"


def read_state(sessions_dir: Path, session_id: str) -> dict:
    """Return the current state.json (defaults if missing)."""
    term = _terminal_dir(sessions_dir, session_id)
    p = term / "state.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {
        "active": False,
        "cwd": None,
        "last_active_at": None,
        "foreground_pid": None,
        "foreground_cmd": None,
        "locked_by": None,
        "shell_pid": None,
    }


def read_log_tail(
    sessions_dir: Path,
    session_id: str,
    limit: int = _DEFAULT_TAIL,
) -> tuple[list[dict], int]:
    """Return (entries, size_bytes). Entries are the last `limit` JSON
    records in log.jsonl; size is the current file size (offset to pass
    back on next call)."""
    term = _terminal_dir(sessions_dir, session_id)
    p = term / "log.jsonl"
    if not p.exists():
        return [], 0
    size = p.stat().st_size
    buf: "deque[str]" = deque(maxlen=max(limit, 1))
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if s:
                    buf.append(s)
    except OSError:
        return [], size
    entries: list[dict] = []
    for s in buf:
        try:
            entries.append(json.loads(s))
        except json.JSONDecodeError:
            pass
    return entries, size


def read_log_from(
    sessions_dir: Path,
    session_id: str,
    offset: int,
) -> tuple[list[dict], int]:
    """Return (entries, new_offset) — the delta since `offset` bytes."""
    term = _terminal_dir(sessions_dir, session_id)
    p = term / "log.jsonl"
    if not p.exists():
        return [], offset
    size = p.stat().st_size
    if offset >= size:
        return [], size
    new_offset = min(size, offset + _MAX_READ_BYTES)
    entries: list[dict] = []
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            data = f.read(new_offset - offset)
    except OSError:
        return [], offset
    for line in data.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            entries.append(json.loads(s))
        except json.JSONDecodeError:
            pass
    return entries, new_offset


def enqueue_input(
    sessions_dir: Path,
    session_id: str,
    *,
    kind: str,
    content: str | None = None,
) -> str:
    """Append one entry to input.jsonl. Returns the entry id.

    `kind` ∈ {"input", "interrupt"}. For "input", `content` is the raw
    text to pass to the shell (caller adds trailing newline if needed).
    """
    if kind not in ("input", "interrupt"):
        raise ValueError(f"unsupported kind: {kind!r}")
    term = _terminal_dir(sessions_dir, session_id)
    term.mkdir(parents=True, exist_ok=True)
    path = _input_queue_path(term)
    entry: dict[str, Any] = {"ts": time.time(), "id": str(uuid.uuid4()), "type": kind}
    if kind == "input":
        entry["content"] = content or ""
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry["id"]


def poll_queue(input_path: Path, offset: int) -> tuple[list[dict], int]:
    """Daemon-side: read new input.jsonl entries at or after `offset`.

    Returns (entries, new_offset). Each entry is a dict with keys
    `{ts, id, type, content?}`; caller dispatches based on `type`.
    """
    if not input_path.exists():
        return [], offset
    with input_path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        data = f.read()
        new_offset = f.tell()
    out: list[dict] = []
    for line in data.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            out.append(json.loads(s))
        except json.JSONDecodeError:
            pass
    return out, new_offset
