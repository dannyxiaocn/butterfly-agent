"""Task card system — JSON-based task files in core/tasks/.

Each task card is a .json file:

    {
      "name": "duty",
      "description": "Review and process child sessions",
      "status": "paused",
      "interval": 3600,
      "created_at": "2026-04-12T10:00:00",
      "last_started_at": null,
      "last_finished_at": null,
      "comments": "",
      "progress": ""
    }

Status values: working | finished | paused
- paused: task is idle, will be triggered when due
- working: task is currently being executed
- finished: task completed (one-shot) or manually finished

Interval: null = one-shot, N = recurring every N seconds.
A recurring task with status=paused fires when:
  last_finished_at is None OR (now - last_finished_at) >= interval
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class TaskCard:
    """A single task card stored as core/tasks/<name>.json."""
    name: str
    description: str = ""
    status: str = "paused"              # paused | working | finished
    interval: float | None = None       # seconds; None = one-shot
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_started_at: str | None = None
    last_finished_at: str | None = None
    comments: str = ""
    progress: str = ""

    def is_due(self, now: datetime | None = None) -> bool:
        """True if this card should fire now."""
        if self.status != "paused":
            return False
        current = now or datetime.now()
        if self.last_finished_at is None:
            return True  # never finished → due immediately
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

        For one-shot tasks (interval is None): stays finished.
        For recurring tasks: status → paused, ready for next interval.
        """
        now_iso = datetime.now().isoformat()
        self.last_finished_at = now_iso
        if self.interval is None:
            self.status = "finished"
        else:
            self.status = "paused"

    def mark_paused(self) -> None:
        self.status = "paused"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "interval": self.interval,
            "created_at": self.created_at,
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "comments": self.comments,
            "progress": self.progress,
        }

    @classmethod
    def from_dict(cls, data: dict, name: str | None = None) -> "TaskCard":
        return cls(
            name=name or data.get("name", "unknown"),
            description=data.get("description", ""),
            status=data.get("status", "paused"),
            interval=data.get("interval"),
            created_at=data.get("created_at", datetime.now().isoformat()),
            last_started_at=data.get("last_started_at"),
            last_finished_at=data.get("last_finished_at") or data.get("last_run_at"),
            comments=data.get("comments", ""),
            progress=data.get("progress", ""),
        )


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
    # Load JSON cards (new format)
    for path in sorted(tasks_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cards.append(TaskCard.from_dict(data, name=path.stem))
        except Exception:
            continue
    # Legacy: also load .md cards for backward compat
    for path in sorted(tasks_dir.glob("*.md")):
        try:
            cards.append(_parse_legacy_md_card(path))
        except Exception:
            continue
    return cards


def load_due_cards(tasks_dir: Path, now: datetime | None = None) -> list[TaskCard]:
    """Return task cards that are due for execution."""
    return [c for c in load_all_cards(tasks_dir) if c.is_due(now)]


def has_pending_cards(tasks_dir: Path) -> bool:
    """True if any task card has status=paused (ready to fire)."""
    return any(c.status == "paused" for c in load_all_cards(tasks_dir))


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
        status="paused",
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
            meta = yaml.safe_load(m.group(1)) or {}
        except Exception:
            meta = {}

    # Map old status values to new
    old_status = meta.get("status", "pending")
    status_map = {"pending": "paused", "running": "working", "completed": "finished"}
    status = status_map.get(old_status, old_status)

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

