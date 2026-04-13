"""Task card system — JSON-based task files in core/tasks/.

Each task card is a .json file:

    {
      "name": "duty",
      "description": "Review and process child sessions",
      "status": "pending",
      "interval": 3600,
      "start_at": "2026-04-12T11:00:00",
      "end_at": "2026-04-19T10:00:00",
      "created_at": "2026-04-12T10:00:00",
      "last_started_at": null,
      "last_finished_at": null,
      "comments": "",
      "progress": ""
    }

Status values: pending | working | finished | paused
- pending: task is waiting for next trigger (auto-state for new & recurring tasks)
- working: task is currently being executed
- finished: task completed (one-shot) or manually finished
- paused: user-initiated pause; won't fire until user explicitly resumes

Interval: null = one-shot, N = recurring every N seconds.

Scheduling:
- start_at: earliest time this task can fire. Default for recurring = created_at + interval;
            for one-shot = created_at (immediate).
- end_at:   auto-expire time. Default = created_at + 7 days; if interval > 7 days then
            created_at + 10 * interval. Hour-level granularity (truncated to the hour).

A task with status=pending fires when:
  now >= start_at AND now < end_at AND
  (last_finished_at is None OR (now - last_finished_at) >= interval)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

_SEVEN_DAYS = 7 * 24 * 3600  # seconds


def _ceil_to_hour(dt: datetime) -> datetime:
    """Round a datetime UP to the next whole hour (unless already exact)."""
    if dt.minute == 0 and dt.second == 0 and dt.microsecond == 0:
        return dt
    return (dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))


def _floor_to_hour(dt: datetime) -> datetime:
    """Truncate a datetime DOWN to the hour."""
    return dt.replace(minute=0, second=0, microsecond=0)


def _default_start_at(created: datetime, interval: float | None) -> str:
    """Compute default start_at (hour-level granularity).

    Recurring: ceil(created + 1 interval) — never earlier than a full interval.
    One-shot: created (immediate).
    """
    if interval is not None and interval > 0:
        raw = created + timedelta(seconds=interval)
        return _ceil_to_hour(raw).isoformat()
    return _floor_to_hour(created).isoformat()


def _default_end_at(created: datetime, interval: float | None) -> str:
    """Compute default end_at (hour-level granularity).

    Default 7 days. If interval > 7 days → 10 * interval instead.
    """
    if interval is not None and interval > _SEVEN_DAYS:
        raw = created + timedelta(seconds=interval * 10)
    else:
        raw = created + timedelta(days=7)
    return _ceil_to_hour(raw).isoformat()


@dataclass
class TaskCard:
    """A single task card stored as core/tasks/<name>.json."""
    name: str
    description: str = ""
    status: str = "pending"             # pending | working | finished | paused
    interval: float | None = None       # seconds; None = one-shot
    start_at: str | None = None         # earliest fire time (ISO; hour granularity)
    end_at: str | None = None           # auto-expire time (ISO; hour granularity)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_started_at: str | None = None
    last_finished_at: str | None = None
    comments: str = ""
    progress: str = ""

    def __post_init__(self) -> None:
        """Fill start_at / end_at defaults if not set."""
        try:
            created = datetime.fromisoformat(self.created_at)
        except (ValueError, TypeError):
            created = datetime.now()
        if self.start_at is None:
            self.start_at = _default_start_at(created, self.interval)
        if self.end_at is None:
            self.end_at = _default_end_at(created, self.interval)

    def is_due(self, now: datetime | None = None) -> bool:
        """True if this card should fire now."""
        if self.status != "pending":
            return False
        current = now or datetime.now()

        # Time window check
        try:
            if self.start_at and current < datetime.fromisoformat(self.start_at):
                return False
        except (ValueError, TypeError):
            pass
        try:
            if self.end_at and current >= datetime.fromisoformat(self.end_at):
                # Auto-expire: mark finished so it won't be checked again
                self.status = "finished"
                return False
        except (ValueError, TypeError):
            pass

        # First-run or interval check
        if self.last_finished_at is None:
            return True  # never finished → due immediately (within window)
        if self.interval is None:
            return False  # one-shot already finished
        try:
            last = datetime.fromisoformat(self.last_finished_at)
            elapsed = (current - last).total_seconds()
            return elapsed >= self.interval
        except (ValueError, TypeError):
            return True

    def mark_working(self) -> None:
        self.status = "working"
        self.last_started_at = datetime.now().isoformat()

    def mark_finished(self) -> None:
        """Mark task as finished after execution.

        For one-shot tasks (interval is None): status → finished (terminal).
        For recurring tasks: status → pending, ready for next interval.
        """
        now_iso = datetime.now().isoformat()
        self.last_finished_at = now_iso
        if self.interval is None:
            self.status = "finished"
        else:
            self.status = "pending"

    def mark_pending(self) -> None:
        """Return task to pending state (e.g. after error recovery)."""
        self.status = "pending"

    def mark_paused(self) -> None:
        """User-initiated pause. Task won't fire until explicitly resumed."""
        self.status = "paused"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "interval": self.interval,
            "start_at": self.start_at,
            "end_at": self.end_at,
            "created_at": self.created_at,
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "comments": self.comments,
            "progress": self.progress,
        }

    @classmethod
    def from_dict(cls, data: dict, name: str | None = None) -> "TaskCard":
        # Normalize legacy status values on load
        raw_status = data.get("status", "pending")
        status = _LEGACY_STATUS_MAP.get(raw_status, raw_status)
        return cls(
            name=name or data.get("name", "unknown"),
            description=data.get("description", ""),
            status=status,
            interval=data.get("interval"),
            start_at=data.get("start_at"),
            end_at=data.get("end_at"),
            created_at=data.get("created_at", datetime.now().isoformat()),
            last_started_at=data.get("last_started_at"),
            last_finished_at=data.get("last_finished_at") or data.get("last_run_at"),
            comments=data.get("comments", ""),
            progress=data.get("progress", ""),
        )


# Legacy status value mapping (from older API/frontend naming)
_LEGACY_STATUS_MAP: dict[str, str] = {
    "running": "working",     # legacy alias
    "completed": "finished",  # legacy alias
}
# Note: "paused" is NOT mapped — it remains a valid status (user-initiated pause).
# Old cards with status="paused" will stay paused; users can resume them via manage_task.


# ── File operations ──────────────────────────────────────────────────────────

def _card_path(tasks_dir: Path, name: str) -> Path:
    safe_name = str(name or "").strip()
    if not safe_name or safe_name in {".", ".."} or "/" in safe_name or "\\" in safe_name:
        raise ValueError(f"invalid task card name: {name!r}")
    return tasks_dir / f"{safe_name}.json"


def save_card(tasks_dir: Path, card: TaskCard) -> Path:
    """Write a task card to disk as JSON. Returns the file path."""
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = _card_path(tasks_dir, card.name)
    path.write_text(
        json.dumps(card.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def load_card(tasks_dir: Path, name: str) -> TaskCard | None:
    """Load one task card by name from core/tasks/."""
    path = _card_path(tasks_dir, name)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return TaskCard.from_dict(data, name=name)
    except Exception:
        return None


def delete_card(tasks_dir: Path, name: str) -> bool:
    """Delete one task card by name. Returns True if it existed."""
    path = _card_path(tasks_dir, name)
    if not path.exists():
        return False
    path.unlink()
    return True


# ── Directory-level operations ──────────────────────────────────────────────

def load_all_cards(tasks_dir: Path) -> list[TaskCard]:
    """Load all task cards from core/tasks/ directory."""
    if not tasks_dir.is_dir():
        return []
    cards = []
    json_names: set[str] = set()
    # Load JSON cards (new format) first
    for path in sorted(tasks_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cards.append(TaskCard.from_dict(data, name=path.stem))
            json_names.add(path.stem)
        except Exception:
            continue
    # Legacy: also load .md cards for backward compat, but skip duplicates
    for path in sorted(tasks_dir.glob("*.md")):
        if path.stem in json_names:
            continue  # JSON version takes precedence
        try:
            cards.append(_parse_legacy_md_card(path))
        except Exception:
            continue
    return cards


def load_due_cards(tasks_dir: Path, now: datetime | None = None) -> list[TaskCard]:
    """Return task cards that are due for execution.

    Side effect: cards that expired (past end_at) are auto-marked finished
    and saved to disk.
    """
    due = []
    for card in load_all_cards(tasks_dir):
        before = card.status
        if card.is_due(now):
            due.append(card)
        elif card.status != before:
            # is_due() changed status (e.g. auto-expired) → persist
            save_card(tasks_dir, card)
    return due


def has_pending_cards(tasks_dir: Path) -> bool:
    """True if any task card has status=pending (ready to fire)."""
    return any(c.status == "pending" for c in load_all_cards(tasks_dir))


def clear_all_cards(tasks_dir: Path) -> None:
    """Mark all cards as finished (used on SESSION_FINISHED)."""
    for card in load_all_cards(tasks_dir):
        card.status = "finished"
        save_card(tasks_dir, card)


def ensure_card(
    tasks_dir: Path,
    name: str,
    interval: float | None = None,
    description: str = "",
    start_at: str | None = None,
    end_at: str | None = None,
) -> TaskCard:
    """Ensure a task card exists. Creates if missing; returns existing or new."""
    tasks_dir.mkdir(parents=True, exist_ok=True)
    existing = load_card(tasks_dir, name)
    if existing is not None:
        return existing
    card = TaskCard(
        name=name,
        description=description,
        interval=interval,
        start_at=start_at,
        end_at=end_at,
        status="pending",
    )
    save_card(tasks_dir, card)
    return card


# ── Legacy compatibility ─────────────────────────────────────────────────────

def _parse_legacy_md_card(path: Path) -> TaskCard:
    """Parse a legacy .md task card with YAML frontmatter."""
    import re
    raw = path.read_text(encoding="utf-8")
    meta: dict = {}
    body = raw

    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", raw, re.DOTALL)
    if m:
        body = raw[m.end():]
        try:
            import yaml
            parsed = yaml.safe_load(m.group(1))
            meta = parsed if isinstance(parsed, dict) else {}
        except Exception:
            meta = {}

    # Map old status values
    old_status = meta.get("status", "pending")
    status = _LEGACY_STATUS_MAP.get(old_status, old_status)

    return TaskCard(
        name=path.stem,
        description=body.strip(),
        status=status,
        interval=meta.get("interval"),
        created_at=meta.get("created_at", datetime.now().isoformat()),
        last_finished_at=meta.get("last_run_at"),
        comments="",
        progress="",
    )


def migrate_legacy_task_sources(session_dir: Path) -> None:
    """Migrate legacy task sources (tasks.md, default_task) into task cards."""
    core_dir = session_dir / "core"
    if not core_dir.exists():
        return
    # Migrate legacy tasks.md
    tasks_md = core_dir / "tasks.md"
    if tasks_md.exists():
        content = tasks_md.read_text(encoding="utf-8").strip()
        tasks_dir = core_dir / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        if content and not any(tasks_dir.glob("*.json")):
            card = TaskCard(name="migrated_task", description=content)
            save_card(tasks_dir, card)
        tasks_md.unlink()
