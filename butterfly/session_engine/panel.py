"""Panel — the session's in-loop work surface.

`sessions/<id>/core/panel/` sits alongside `core/tasks/` and records per-call
state for non-blocking tools (and, later, sub-agent references). One `.json`
file per entry, keyed by the stable `tid`.

This module owns:
- The PanelEntry schema (dataclass + serialization)
- Filesystem helpers for listing / loading / saving entries
- Status constants

The BackgroundTaskManager (butterfly/tool_engine/background.py) is the primary
writer; the daemon loop and UI are readers. See
`docs/butterfly/tool_engine/design.md` §5 for the full design.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ── Status constants ──────────────────────────────────────────────────────────

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_STALLED = "stalled"
STATUS_KILLED = "killed"
STATUS_KILLED_BY_RESTART = "killed_by_restart"

TERMINAL_STATUSES = frozenset({
    STATUS_COMPLETED,
    STATUS_KILLED,
    STATUS_KILLED_BY_RESTART,
})

# "stalled" is not terminal — the task may still produce output and transition
# to completed; it's just a notification about a silent period.

# ── Entry types ───────────────────────────────────────────────────────────────

TYPE_PENDING_TOOL = "pending_tool"
TYPE_SUB_AGENT = "sub_agent"

VALID_ENTRY_TYPES = frozenset({TYPE_PENDING_TOOL, TYPE_SUB_AGENT})


# ── PanelEntry dataclass ──────────────────────────────────────────────────────

@dataclass
class PanelEntry:
    """Schema for a single panel entry on disk.

    Keep field names stable — both UI and daemon read these. See
    `docs/butterfly/tool_engine/design.md` §5.1 for semantics.
    """

    tid: str
    type: str
    tool_name: str
    input: dict[str, Any]

    status: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None

    polling_interval: int | None = None
    last_delivered_bytes: int = 0
    last_activity_at: float | None = None

    pid: int | None = None
    exit_code: int | None = None

    output_file: str | None = None
    output_bytes: int = 0

    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "PanelEntry":
        # Tolerant to extra fields (forward compat).
        known = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


# ── TID generation ────────────────────────────────────────────────────────────

def new_tid(prefix: str = "bg") -> str:
    """Generate a stable, short task id. Used as both dict key and filename stem.

    4 hex bytes = 32 bits of entropy; long-lived sessions can accumulate many
    panel entries, and 24-bit ids had a non-negligible birthday-collision
    chance once you got into the hundreds.
    """
    return f"{prefix}_{secrets.token_hex(4)}"


# ── Filesystem helpers ────────────────────────────────────────────────────────

def entry_path(panel_dir: Path, tid: str) -> Path:
    return panel_dir / f"{tid}.json"


def save_entry(panel_dir: Path, entry: PanelEntry) -> Path:
    """Atomically write entry to panel_dir/<tid>.json."""
    panel_dir.mkdir(parents=True, exist_ok=True)
    path = entry_path(panel_dir, entry.tid)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(entry.to_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def load_entry(panel_dir: Path, tid: str) -> PanelEntry | None:
    path = entry_path(panel_dir, tid)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # from_json can raise TypeError on a truncated / schema-invalid blob
    # (e.g. missing required dataclass fields). Treat as "not loadable".
    try:
        return PanelEntry.from_json(data)
    except (TypeError, ValueError, KeyError):
        return None


def list_entries(panel_dir: Path) -> list[PanelEntry]:
    """Return all panel entries, sorted by created_at ascending. Skips files
    that are JSON-invalid or schema-invalid rather than crashing the listing."""
    if not panel_dir.is_dir():
        return []
    entries: list[PanelEntry] = []
    for p in sorted(panel_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        try:
            entries.append(PanelEntry.from_json(data))
        except (TypeError, ValueError, KeyError):
            continue
    entries.sort(key=lambda e: e.created_at)
    return entries


def create_pending_tool_entry(
    panel_dir: Path,
    *,
    tool_name: str,
    input: dict[str, Any],
    polling_interval: int | None = None,
    meta: dict[str, Any] | None = None,
    entry_type: str = TYPE_PENDING_TOOL,
) -> PanelEntry:
    """Create + persist a new panel entry. Returns the entry.

    `entry_type` defaults to TYPE_PENDING_TOOL for back-compat with the original
    bash background flow; sub-agent spawns pass TYPE_SUB_AGENT. An invalid
    entry_type raises ValueError so downstream filters that switch on type
    can't be silently broken by a typo (cubic review, PR #28).
    """
    if entry_type not in VALID_ENTRY_TYPES:
        raise ValueError(
            f"create_pending_tool_entry: entry_type must be one of "
            f"{sorted(VALID_ENTRY_TYPES)}, got {entry_type!r}"
        )
    now = time.time()
    entry = PanelEntry(
        tid=new_tid("bg"),
        type=entry_type,
        tool_name=tool_name,
        input=input,
        status=STATUS_RUNNING,
        created_at=now,
        started_at=now,
        polling_interval=polling_interval,
        last_activity_at=now,
        meta=meta or {},
    )
    save_entry(panel_dir, entry)
    return entry


def sweep_killed_by_restart(panel_dir: Path) -> list[PanelEntry]:
    """Mark every non-terminal entry as killed_by_restart.

    Covers both `running` and `stalled` — the latter also holds an orphan
    subprocess from our POV after a daemon restart (a stalled entry means the
    old manager had hit the 5-min watchdog but the process was still alive).

    Called once when the server/daemon starts. Returns the updated entries so
    the caller can emit notifications for each.
    """
    non_terminal = {STATUS_RUNNING, STATUS_STALLED}
    updated: list[PanelEntry] = []
    for entry in list_entries(panel_dir):
        if entry.status in non_terminal:
            entry.status = STATUS_KILLED_BY_RESTART
            entry.finished_at = time.time()
            save_entry(panel_dir, entry)
            updated.append(entry)
    return updated
