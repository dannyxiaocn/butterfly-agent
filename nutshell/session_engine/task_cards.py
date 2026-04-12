"""Task card system — replaces single tasks.md with per-task files in core/tasks/.

Each task card is a .md file with YAML frontmatter:

    ---
    interval: 3600        # seconds between runs; null = one-shot
    starts_at: null       # optional ISO timestamp before which the task will not run
    ends_at: null         # optional ISO timestamp after which the task will not run
    status: pending       # pending | running | completed | paused
    last_run_at: null     # ISO timestamp of last execution
    created_at: 2026-04-08T12:00:00
    ---

    Task instructions here...

The heartbeat is a special recurring task card named "heartbeat". Legacy
default_task values are migrated into this card so task cards remain the single
task source of truth.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from nutshell.session_engine.session_config import read_config, write_config


@dataclass
class TaskCard:
    """A single task card read from core/tasks/<name>.md."""
    name: str
    content: str
    interval: float | None = None   # seconds; None = one-shot
    starts_at: str | None = None
    ends_at: str | None = None
    status: str = "pending"         # pending | running | completed | paused
    last_run_at: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def is_due(self, now: datetime | None = None) -> bool:
        """True if this card should fire now."""
        if self.status != "pending":
            return False
        current = _current_time(now, self.starts_at, self.ends_at, self.last_run_at)
        starts_at = _parse_iso(self.starts_at)
        if starts_at is not None:
            try:
                if current < starts_at:
                    return False
            except TypeError:
                return False
        ends_at = _parse_iso(self.ends_at)
        if ends_at is not None:
            try:
                if current > ends_at:
                    return False
            except TypeError:
                return False
        if self.last_run_at is None:
            return True  # never run → due immediately
        if self.interval is None:
            return False  # one-shot already ran
        try:
            last = datetime.fromisoformat(self.last_run_at)
            elapsed = (current - last).total_seconds()
            return elapsed >= self.interval
        except (ValueError, TypeError):
            return True

    def mark_running(self) -> None:
        self.status = "running"

    def mark_done(self, *, clear: bool = False) -> None:
        """Mark task as finished after one execution.

        For one-shot tasks (interval is None): status → completed.
        For recurring tasks: status → pending, last_run_at updated.
        If clear=True: status → completed regardless (SESSION_FINISHED).
        """
        now_iso = datetime.now().isoformat()
        self.last_run_at = now_iso
        if clear or self.interval is None:
            self.status = "completed"
        else:
            self.status = "pending"


# ── Frontmatter parsing ────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _current_time(now: datetime | None, *reference_values: str | None) -> datetime:
    if now is not None:
        return now
    for value in reference_values:
        parsed = _parse_iso(value)
        if parsed is not None:
            return datetime.now(parsed.tzinfo) if parsed.tzinfo is not None else datetime.now()
    return datetime.now()


def _card_path(tasks_dir: Path, name: str) -> Path:
    safe_name = str(name or "").strip()
    if not safe_name or safe_name in {".", ".."} or "/" in safe_name or "\\" in safe_name:
        raise ValueError(f"invalid task card name: {name!r}")
    return tasks_dir / f"{safe_name}.md"


def _parse_card_file(path: Path) -> TaskCard:
    """Parse a task card .md file with YAML frontmatter."""
    raw = path.read_text(encoding="utf-8")
    meta: dict = {}
    body = raw

    m = _FRONTMATTER_RE.match(raw)
    if m:
        body = raw[m.end():]
        try:
            import yaml
            meta = yaml.safe_load(m.group(1)) or {}
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}

    name = path.stem
    return TaskCard(
        name=name,
        content=body.strip(),
        interval=meta.get("interval"),
        starts_at=meta.get("starts_at"),
        ends_at=meta.get("ends_at"),
        status=meta.get("status", "pending"),
        last_run_at=meta.get("last_run_at"),
        created_at=meta.get("created_at", datetime.now().isoformat()),
    )


def _serialize_card(card: TaskCard) -> str:
    """Serialize a TaskCard back to .md with YAML frontmatter."""
    import yaml
    meta: dict = {
        "interval": card.interval,
        "starts_at": card.starts_at,
        "ends_at": card.ends_at,
        "status": card.status,
        "last_run_at": card.last_run_at,
        "created_at": card.created_at,
    }
    header = yaml.dump(meta, default_flow_style=False, allow_unicode=True).strip()
    return f"---\n{header}\n---\n\n{card.content}\n"


def save_card(tasks_dir: Path, card: TaskCard) -> Path:
    """Write a task card to disk. Returns the file path."""
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = _card_path(tasks_dir, card.name)
    path.write_text(_serialize_card(card), encoding="utf-8")
    return path


def load_card(tasks_dir: Path, name: str) -> TaskCard | None:
    """Load one task card by name from core/tasks/."""
    path = _card_path(tasks_dir, name)
    if not path.is_file():
        return None
    return _parse_card_file(path)


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
    for path in sorted(tasks_dir.glob("*.md")):
        try:
            cards.append(_parse_card_file(path))
        except Exception:
            continue
    return cards


def load_due_cards(tasks_dir: Path, now: datetime | None = None) -> list[TaskCard]:
    """Return task cards that are due for execution."""
    return [c for c in load_all_cards(tasks_dir) if c.is_due(now)]


def has_pending_cards(tasks_dir: Path) -> bool:
    """True if any task card has status=pending."""
    return any(c.status == "pending" for c in load_all_cards(tasks_dir))


def clear_all_cards(tasks_dir: Path) -> None:
    """Mark all cards as completed (used on SESSION_FINISHED)."""
    for card in load_all_cards(tasks_dir):
        card.status = "completed"
        save_card(tasks_dir, card)


# ── Migration ───────────────────────────────────────────────────────────────

def migrate_tasks_md(core_dir: Path) -> None:
    """Migrate legacy core/tasks.md to core/tasks/ directory if needed.

    If tasks.md has content and core/tasks/ doesn't exist or is empty,
    creates a one-shot task card from the content. Then removes tasks.md.
    """
    tasks_md = core_dir / "tasks.md"
    tasks_dir = core_dir / "tasks"

    if not tasks_md.exists():
        return

    content = tasks_md.read_text(encoding="utf-8").strip()
    tasks_dir.mkdir(parents=True, exist_ok=True)

    if content and not any(tasks_dir.glob("*.md")):
        card = TaskCard(
            name="migrated_task",
            content=content,
            interval=None,  # one-shot
        )
        save_card(tasks_dir, card)

    # Remove legacy file
    tasks_md.unlink()


def migrate_legacy_default_task(session_dir: Path) -> None:
    """Move legacy params.default_task into the heartbeat task card."""
    params = read_config(session_dir)
    legacy_default_task = params.get("default_task")
    if legacy_default_task in (None, ""):
        return

    tasks_dir = session_dir / "core" / "tasks"
    heartbeat_interval = float(params.get("heartbeat_interval") or 7200.0)
    heartbeat = load_card(tasks_dir, "heartbeat")
    if heartbeat is None:
        ensure_heartbeat_card(tasks_dir, interval=heartbeat_interval, content=str(legacy_default_task))
    else:
        changed = False
        if not (heartbeat.content or "").strip():
            heartbeat.content = str(legacy_default_task)
            changed = True
        if heartbeat.interval is None:
            heartbeat.interval = heartbeat_interval
            changed = True
        if changed:
            save_card(tasks_dir, heartbeat)

    write_config(session_dir, default_task=None)


def migrate_legacy_task_sources(session_dir: Path) -> None:
    """Migrate legacy task sources into task cards."""
    core_dir = session_dir / "core"
    if not core_dir.exists():
        return
    migrate_tasks_md(core_dir)
    migrate_legacy_default_task(session_dir)


# ── Heartbeat card helpers ──────────────────────────────────────────────────

_DEFAULT_HEARTBEAT_CONTENT = (
    "Check for incoming messages from other agents. "
    "Review your current state. If nothing needs attention, rest."
)


def ensure_heartbeat_card(
    tasks_dir: Path,
    interval: float,
    content: str | None = None,
) -> TaskCard:
    """Ensure a heartbeat task card exists for persistent sessions.

    Creates the card if missing; returns the existing or new card.
    """
    tasks_dir.mkdir(parents=True, exist_ok=True)
    heartbeat_path = tasks_dir / "heartbeat.md"
    if heartbeat_path.exists():
        return _parse_card_file(heartbeat_path)

    card = TaskCard(
        name="heartbeat",
        content=content or _DEFAULT_HEARTBEAT_CONTENT,
        interval=interval,
        status="pending",
    )
    save_card(tasks_dir, card)
    return card
