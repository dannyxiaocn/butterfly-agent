"""Event primitives for the refactored runtime.

Phase 1 of web-ui-refactor. See `docs/refactor/DESIGN.md` §2 (invariants),
§3 (schema), §5.2 (API), §5.6 (write semantics).

Single source of truth for `events_v1.jsonl` under each session directory.
The `_v1` suffix is deliberate — Phase 3 migration renames to `events.jsonl`
after converting legacy format. For now we sit side-by-side with the old file.

Invariants: I1 append-only. I8 ids monotonic integers starting at 1.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Iterable, Iterator

logger = logging.getLogger(__name__)


# ── Events file name ──────────────────────────────────────────────────────────

EVENTS_FILENAME = "events_v1.jsonl"


# ── Event type constants ──────────────────────────────────────────────────────

# User-side (for_llm=True)
EVENT_USER_INPUT = "user_input"
EVENT_USER_INTERRUPT = "user_interrupt"

# Agent-side (for_llm=True)
EVENT_AGENT_TEXT = "agent_text"
EVENT_AGENT_THINKING = "agent_thinking"
EVENT_AGENT_TOOL_CALL = "agent_tool_call"
EVENT_AGENT_TOOL_RESULT = "agent_tool_result"

# Agent-side UI lifecycle markers (for_llm=False — purely presentation;
# do NOT enter the LLM context). These restore the two-phase rendering the
# pre-PR-#57 frontend depended on:
#   * `agent_thinking_start` — provider opened a thinking stream; UI shows
#     the spinning "Thinking…" cell. Paired with the canonical
#     `agent_thinking` (still for_llm=True) which marks the close.
#   * `agent_bg_tool_dispatched` — bg-spawn tool returned its placeholder
#     ("task_id=…") and the panel will track it; UI keeps the cell yellow
#     until the deferred `agent_tool_result` lands. Lets the old
#     tool_done(placeholder) → tool_finalize(actual) UX work without
#     breaking the events_v1 invariant that agent_tool_result is the
#     single canonical result event.
EVENT_AGENT_THINKING_START = "agent_thinking_start"
EVENT_AGENT_BG_TOOL_DISPATCHED = "agent_bg_tool_dispatched"

# System-side (for_llm=False)
EVENT_SESSION_CREATED = "session_created"
EVENT_SESSION_STARTED = "session_started"
EVENT_SESSION_STOPPED = "session_stopped"
EVENT_SESSION_DELETED = "session_deleted"
EVENT_MODEL_STATUS = "model_status"
EVENT_LLM_CALL_USAGE = "llm_call_usage"
EVENT_TOOL_PROGRESS = "tool_progress"
EVENT_TASK_CARD_CHANGED = "task_card_changed"
EVENT_TASK_SCRIPT_CHECK = "task_script_check"
EVENT_TASK_SCRIPT_ERROR = "task_script_error"
EVENT_TASK_FINISHED = "task_finished"
EVENT_TODO_LIST_CHANGED = "todo_list_changed"
EVENT_TERMINAL_LOG = "terminal_log"
EVENT_TERMINAL_STATE = "terminal_state"
EVENT_TERMINAL_INPUT = "terminal_input"
EVENT_PANEL_ENTRY_CHANGED = "panel_entry_changed"
EVENT_SUB_AGENT_COUNT = "sub_agent_count"
EVENT_CONFIG_CHANGED = "config_changed"
EVENT_PROMPT_CHANGED = "prompt_changed"
EVENT_ASSET_CHANGED = "asset_changed"
EVENT_SYSTEM_NOTICE = "system_notice"
EVENT_ERROR = "error"
EVENT_CONTROL_INTERRUPT = "control_interrupt"
EVENT_CONTROL_START = "control_start"
EVENT_CONTROL_STOP = "control_stop"


# ── user_input `source` enum (DESIGN.md §3.3) ─────────────────────────────────

# Callers should reference these constants rather than the raw string literals
# so renames sweep cleanly and `test_subagent_rename_sweep` doesn't flag the
# schema enum (distinct from the retired `sub_agent` tool-name literal).
SOURCE_CLI = "cli"
SOURCE_WEB = "web"
SOURCE_TASK = "task"
SOURCE_SUBAGENT = "sub_agent"  # noqa: Q000 - schema enum value, not tool name


# ── `for_llm` default taxonomy ────────────────────────────────────────────────

# Mirrors DESIGN.md §3.3-§3.5. Callers of `append_event` do not need to pass
# `for_llm`; the module resolves from the type. Unknown types default to False
# and emit a single WARNING.
_FOR_LLM_DEFAULTS: dict[str, bool] = {
    # user-side
    EVENT_USER_INPUT: True,
    EVENT_USER_INTERRUPT: True,
    # agent-side
    EVENT_AGENT_TEXT: True,
    EVENT_AGENT_THINKING: True,
    EVENT_AGENT_TOOL_CALL: True,
    EVENT_AGENT_TOOL_RESULT: True,
    # UI lifecycle markers (for_llm=False — see constant block above)
    EVENT_AGENT_THINKING_START: False,
    EVENT_AGENT_BG_TOOL_DISPATCHED: False,
    # system-side
    EVENT_SESSION_CREATED: False,
    EVENT_SESSION_STARTED: False,
    EVENT_SESSION_STOPPED: False,
    EVENT_SESSION_DELETED: False,
    EVENT_MODEL_STATUS: False,
    EVENT_LLM_CALL_USAGE: False,
    EVENT_TOOL_PROGRESS: False,
    EVENT_TASK_CARD_CHANGED: False,
    EVENT_TASK_SCRIPT_CHECK: False,
    EVENT_TASK_SCRIPT_ERROR: False,
    EVENT_TASK_FINISHED: False,
    EVENT_TODO_LIST_CHANGED: False,
    EVENT_TERMINAL_LOG: False,
    EVENT_TERMINAL_STATE: False,
    EVENT_TERMINAL_INPUT: False,
    EVENT_PANEL_ENTRY_CHANGED: False,
    EVENT_SUB_AGENT_COUNT: False,
    EVENT_CONFIG_CHANGED: False,
    EVENT_PROMPT_CHANGED: False,
    EVENT_ASSET_CHANGED: False,
    EVENT_SYSTEM_NOTICE: False,
    EVENT_ERROR: False,
    EVENT_CONTROL_INTERRUPT: False,
    EVENT_CONTROL_START: False,
    EVENT_CONTROL_STOP: False,
}


# ── Event dataclass ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Event:
    """One line of events_v1.jsonl. See DESIGN.md §3.1."""

    id: int
    ts: float
    type: str
    for_llm: bool
    payload: dict

    def to_dict(self) -> dict:
        """JSON-serializable dict with exactly the schema keys."""
        return {
            "id": self.id,
            "ts": self.ts,
            "type": self.type,
            "for_llm": self.for_llm,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(
            id=int(d["id"]),
            ts=float(d["ts"]),
            type=str(d["type"]),
            for_llm=bool(d["for_llm"]),
            payload=dict(d.get("payload") or {}),
        )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _events_path(session_dir: Path) -> Path:
    return Path(session_dir) / EVENTS_FILENAME


def _resolve_for_llm(event_type: str, explicit: bool | None) -> bool:
    """Resolve the `for_llm` flag, warning once per unknown type."""
    if explicit is not None:
        return bool(explicit)
    if event_type in _FOR_LLM_DEFAULTS:
        return _FOR_LLM_DEFAULTS[event_type]
    logger.warning(
        "events.append_event: unknown event type %r — defaulting for_llm=False. "
        "Add it to _FOR_LLM_DEFAULTS in butterfly/runtime/events.py.",
        event_type,
    )
    return False


def _scan_last_id(path: Path) -> int:
    """Return the max id already in the file, or 0 if empty / absent.

    TODO(perf): O(n) full re-scan. A tail-seek + reverse-scan would be O(1)
    for typical append workloads. Phase 12 material.
    """
    if not path.exists():
        return 0
    last = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    # Malformed line — skip; Phase 12 should surface an error.
                    continue
                eid = d.get("id")
                if isinstance(eid, int) and eid > last:
                    last = eid
    except FileNotFoundError:
        return 0
    return last


# ── Primitives ────────────────────────────────────────────────────────────────

def append_event(
    session_dir: Path,
    event_type: str,
    payload: dict,
    *,
    for_llm: bool | None = None,
    ts: float | None = None,
) -> Event:
    """Append one event to `<session_dir>/events_v1.jsonl`.

    Write semantics (DESIGN.md §5.6):
      1. Acquire LOCK_EX on the events file.
      2. Compute next id from the last line's id + 1 (starts at 1).
      3. Write one compact JSON line + `\\n`.
      4. Flush + fsync.
      5. Release the lock.

    Returns the materialized `Event` with its assigned id.
    """
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)

    path = _events_path(session_dir)
    resolved_for_llm = _resolve_for_llm(event_type, for_llm)
    resolved_ts = time.time() if ts is None else float(ts)
    resolved_payload = dict(payload or {})

    # `a+b` creates if missing, positions at EOF for writes. We manage our
    # own framing — one JSON object + '\n' per event.
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            next_id = _scan_last_id(path) + 1
            event = Event(
                id=next_id,
                ts=resolved_ts,
                type=event_type,
                for_llm=resolved_for_llm,
                payload=resolved_payload,
            )
            line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
            data = (line + "\n").encode("utf-8")
            os.write(fd, data)
            os.fsync(fd)
            return event
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def read_events(
    session_dir: Path,
    *,
    since_id: int | None = None,
    until_id: int | None = None,
    types: Iterable[str] | None = None,
) -> Iterator[Event]:
    """Read events from disk, optionally filtered.

    Filters:
      - `since_id` is EXCLUSIVE: yields events with `id > since_id`.
      - `until_id` is INCLUSIVE: yields events with `id <= until_id`.
      - `types`: if provided, only events whose `type` is in the set.

    Tolerates missing file / empty file (yields nothing). Does NOT hold the
    file handle open across yields — reads the whole file and returns an
    iterator over the decoded list. Performance tuning is Phase 12 work.
    """
    path = _events_path(Path(session_dir))
    if not path.exists():
        return iter(())

    type_set: set[str] | None = set(types) if types is not None else None

    decoded: list[Event] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    ev = Event.from_dict(d)
                except (KeyError, TypeError, ValueError):
                    continue
                if since_id is not None and ev.id <= since_id:
                    continue
                if until_id is not None and ev.id > until_id:
                    continue
                if type_set is not None and ev.type not in type_set:
                    continue
                decoded.append(ev)
    except FileNotFoundError:
        return iter(())

    return iter(decoded)


def latest_event_id(session_dir: Path) -> int:
    """Return the largest id in events_v1.jsonl, or 0 if file absent/empty."""
    return _scan_last_id(_events_path(Path(session_dir)))


async def tail_events(
    session_dir: Path,
    *,
    cursor: int | None = None,
    timeout: float = 30.0,
    poll_interval: float = 0.1,
) -> AsyncIterator[Event]:
    """Async-yield new events with id > cursor as they are appended.

    - `cursor=None` treats as 0 (yield everything from the start).
    - Polls the file every `poll_interval` seconds.
    - Stops yielding after `timeout` seconds with no new events; the caller
      can reconnect if desired. Each yielded event resets the idle deadline.
    - Uses `asyncio.sleep` — no threads.
    """
    session_dir = Path(session_dir)
    last_seen = cursor if cursor is not None else 0
    deadline = time.monotonic() + timeout

    while True:
        new_events = list(read_events(session_dir, since_id=last_seen))
        if new_events:
            for ev in new_events:
                yield ev
                last_seen = ev.id
            deadline = time.monotonic() + timeout
            # Loop again immediately — more may have arrived while we yielded.
            continue

        if time.monotonic() >= deadline:
            return

        await asyncio.sleep(poll_interval)
