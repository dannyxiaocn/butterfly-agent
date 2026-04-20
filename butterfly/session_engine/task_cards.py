"""Task card system — bash-driven trigger/end (v2.0.27).

Each card has two parts:

    core/tasks/<name>.json          status + metadata
    core/tasks/<name>.trigger.sh    bash check run every ``check_interval``
                                    seconds. Last line must be one of:
                                      [start]            — activate the agent
                                      [start] <message>  — activate with <message>
                                                            appended as the seed
                                      [skip]             — do nothing, recheck
                                                            next interval
    core/tasks/<name>.end.sh        optional; polled while status == working.
                                    Last line:
                                      [done]             — mark card finished
                                      [not_done]         — keep running

Unknown output / non-zero exit → treated as ``[skip]`` for trigger and
``[not_done]`` for end, logged as a ``task_check_error`` via the runtime.

Status values
-------------
    pending   waiting for the next trigger_script check
    working   agent is currently running the card's wakeup
    finished  card completed (one-shot agent ``task_finish`` tool or ``[done]``)
    paused    user-initiated pause; won't fire until explicitly resumed

Time semantics
--------------
No ``start_at`` / ``end_at`` / ``interval`` fields — all time gating lives
inside the trigger_script. ``check_interval`` is purely "how often do we
RE-RUN the check script"; its last_checked_at tracks the last run so the
runtime can skip cards whose interval hasn't elapsed yet. For a card that
wants to fire every N seconds, the trigger script is literally ``echo [start]``
(or empty — the polling cadence does the work).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


# Sentinel prefixes emitted by trigger / end scripts (must be the last line
# of stdout). Anything after the tag on the same line is carried as a
# "reason" — trigger can use it as the agent's seed input.
_TRIGGER_FIRE = "[start]"
_TRIGGER_SKIP = "[skip]"
_END_DONE = "[done]"
_END_NOT_DONE = "[not_done]"


@dataclass
class ScriptResult:
    """Outcome of running a trigger/end script."""
    tag: str | None             # one of: [start] / [skip] / [done] / [not_done] / None (unparseable)
    message: str                # text after the tag on the same line (may be empty)
    stdout: str                 # full captured stdout (may be truncated upstream)
    stderr: str
    exit_code: int | None
    duration_ms: int
    timed_out: bool = False


def parse_trigger_output(stdout: str, exit_code: int | None) -> tuple[str, str] | None:
    """Parse last-line of trigger_script stdout.

    Returns (tag, message) where tag ∈ {"[start]", "[skip]"} on clean
    output. Returns ``None`` if the script exited non-zero OR the last
    non-empty line does not begin with a recognised tag — callers treat
    both as fail-closed ``skip`` and emit an error event.
    """
    if exit_code not in (0, None):
        return None
    tag, msg = _last_line_tag(stdout)
    if tag in (_TRIGGER_FIRE, _TRIGGER_SKIP):
        return tag, msg
    return None


def parse_end_output(stdout: str, exit_code: int | None) -> tuple[str, str] | None:
    """Parse last-line of end_script stdout.

    Returns (tag, message) where tag ∈ {"[done]", "[not_done]"} on clean
    output. Returns ``None`` on fail-closed (script error / bad output) —
    end_script errors are conservative: no spurious ``mark_finished``.
    """
    if exit_code not in (0, None):
        return None
    tag, msg = _last_line_tag(stdout)
    if tag in (_END_DONE, _END_NOT_DONE):
        return tag, msg
    return None


def _last_line_tag(stdout: str) -> tuple[str | None, str]:
    """Return (tag, message) from the last non-empty line of stdout.

    Tag is None when the line doesn't start with a recognised marker.
    """
    if not stdout:
        return None, ""
    # iterate in reverse to find the last non-empty line
    last_line = ""
    for line in reversed(stdout.splitlines()):
        s = line.strip()
        if s:
            last_line = s
            break
    if not last_line:
        return None, ""
    for tag in (_TRIGGER_FIRE, _TRIGGER_SKIP, _END_DONE, _END_NOT_DONE):
        if last_line == tag:
            return tag, ""
        if last_line.startswith(tag + " "):
            return tag, last_line[len(tag) + 1:].strip()
    return None, last_line


# ── Dataclass ─────────────────────────────────────────────────────────────────

_DEFAULT_CHECK_INTERVAL = 3600.0


@dataclass
class TaskCard:
    """A single task card stored as ``core/tasks/<name>.json``."""
    name: str
    description: str = ""
    status: str = "pending"             # pending | working | finished | paused
    check_interval: float = _DEFAULT_CHECK_INTERVAL  # seconds between trigger runs
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_checked_at: str | None = None
    last_started_at: str | None = None
    last_finished_at: str | None = None
    comments: str = ""
    progress: str = ""

    def needs_check(self, now: datetime | None = None) -> bool:
        """True when the trigger_script is due to be polled.

        Cards in ``working`` / ``finished`` / ``paused`` never check the
        TRIGGER script (end_script is handled separately). For ``pending``
        cards: fire the very first check immediately on first sight, then
        throttle to one check per ``check_interval`` seconds.
        """
        if self.status != "pending":
            return False
        if self.last_checked_at is None:
            return True
        current = now or datetime.now()
        try:
            last = datetime.fromisoformat(self.last_checked_at)
        except (ValueError, TypeError):
            return True
        return (current - last).total_seconds() >= float(self.check_interval or 0)

    def end_check_due(self, now: datetime | None = None) -> bool:
        """Same cadence as ``needs_check`` but for the end_script.

        Only relevant while status == ``working``. Reuses the same
        ``last_checked_at`` bookkeeping so the trigger + end checks share a
        single cadence (they're mutually exclusive by status anyway).
        """
        if self.status != "working":
            return False
        if self.last_checked_at is None:
            return True
        current = now or datetime.now()
        try:
            last = datetime.fromisoformat(self.last_checked_at)
        except (ValueError, TypeError):
            return True
        return (current - last).total_seconds() >= float(self.check_interval or 0)

    def mark_checked(self, now: datetime | None = None) -> None:
        """Stamp last_checked_at — called right after the script runs."""
        self.last_checked_at = (now or datetime.now()).isoformat()

    def mark_working(self) -> None:
        self.status = "working"
        self.last_started_at = datetime.now().isoformat()

    def mark_finished(self) -> None:
        """Mark task as finished after execution.

        Cards return to ``pending`` so the trigger_script keeps polling —
        the agent's trigger script decides whether to fire again. A truly
        one-shot card is expressed by a trigger script that emits
        ``[start]`` once and ``[skip]`` forever after (e.g. gating on a
        file or marker the card itself touches on completion).
        """
        self.last_finished_at = datetime.now().isoformat()
        self.status = "pending"

    def mark_terminal(self) -> None:
        """Force-finalise a card (used by ``task_finish`` tool / ``[done]``)."""
        self.last_finished_at = datetime.now().isoformat()
        self.status = "finished"

    def mark_pending(self) -> None:
        """Return task to pending (e.g. after error recovery)."""
        self.status = "pending"

    def mark_paused(self) -> None:
        """User-initiated pause. Task won't fire until explicitly resumed."""
        self.status = "paused"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "check_interval": self.check_interval,
            "created_at": self.created_at,
            "last_checked_at": self.last_checked_at,
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "comments": self.comments,
            "progress": self.progress,
        }

    @classmethod
    def from_dict(cls, data: dict, name: str | None = None) -> "TaskCard":
        """Load a card from its JSON. Tolerates pre-2.0.27 fields by
        dropping them (start_at / end_at / interval). A legacy ``interval``
        is forwarded as ``check_interval`` so operators with existing
        sessions don't lose their polling cadence.
        """
        check_interval = data.get("check_interval")
        if check_interval is None:
            legacy_interval = data.get("interval")
            check_interval = (
                float(legacy_interval)
                if isinstance(legacy_interval, (int, float)) and legacy_interval > 0
                else _DEFAULT_CHECK_INTERVAL
            )
        return cls(
            name=name or data.get("name", "unknown"),
            description=data.get("description", ""),
            status=data.get("status", "pending"),
            check_interval=float(check_interval),
            created_at=data.get("created_at", datetime.now().isoformat()),
            last_checked_at=data.get("last_checked_at"),
            last_started_at=data.get("last_started_at"),
            last_finished_at=data.get("last_finished_at"),
            comments=data.get("comments", ""),
            progress=data.get("progress", ""),
        )


# ── File paths ────────────────────────────────────────────────────────────────

def _card_path(tasks_dir: Path, name: str) -> Path:
    safe_name = str(name or "").strip()
    if not safe_name or safe_name in {".", ".."} or "/" in safe_name or "\\" in safe_name:
        raise ValueError(f"invalid task card name: {name!r}")
    return tasks_dir / f"{safe_name}.json"


def trigger_script_path(tasks_dir: Path, name: str) -> Path:
    return tasks_dir / f"{name}.trigger.sh"


def end_script_path(tasks_dir: Path, name: str) -> Path:
    return tasks_dir / f"{name}.end.sh"


def save_card(tasks_dir: Path, card: TaskCard) -> Path:
    """Write a task card JSON to disk."""
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = _card_path(tasks_dir, card.name)
    path.write_text(
        json.dumps(card.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def write_trigger_script(tasks_dir: Path, name: str, body: str) -> Path:
    """Write ``body`` to ``<name>.trigger.sh`` (mode 0644). Body stored
    verbatim — the agent owns the script content. Empty body is allowed
    (equivalent to ``echo [start]`` for the degenerate "always fire" case)
    and is normalised to that one-liner so the parser sees a valid tag.
    """
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = trigger_script_path(tasks_dir, name)
    text = (body or "").strip() or f"echo {_TRIGGER_FIRE}"
    if not text.startswith("#!"):
        text = "#!/bin/bash\n" + text
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")
    return path


def write_end_script(tasks_dir: Path, name: str, body: str | None) -> Path | None:
    """Write ``body`` to ``<name>.end.sh``. ``None`` removes any existing
    end script (so updating a card to drop its end condition is explicit).
    """
    path = end_script_path(tasks_dir, name)
    if body is None or not body.strip():
        if path.exists():
            path.unlink()
        return None
    tasks_dir.mkdir(parents=True, exist_ok=True)
    text = body.strip()
    if not text.startswith("#!"):
        text = "#!/bin/bash\n" + text
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")
    return path


def read_trigger_script(tasks_dir: Path, name: str) -> str | None:
    """Return trigger.sh body (including shebang) or None when absent."""
    path = trigger_script_path(tasks_dir, name)
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def read_end_script(tasks_dir: Path, name: str) -> str | None:
    path = end_script_path(tasks_dir, name)
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


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
    """Delete a card's JSON and any trigger/end scripts beside it."""
    path = _card_path(tasks_dir, name)
    existed = path.exists()
    if existed:
        path.unlink()
    for side in (trigger_script_path(tasks_dir, name), end_script_path(tasks_dir, name)):
        if side.exists():
            side.unlink()
    return existed


# ── Directory-level helpers ───────────────────────────────────────────────────

def load_all_cards(tasks_dir: Path) -> list[TaskCard]:
    """Load all cards under ``tasks_dir`` sorted by filename."""
    if not tasks_dir.is_dir():
        return []
    cards: list[TaskCard] = []
    for path in sorted(tasks_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cards.append(TaskCard.from_dict(data, name=path.stem))
        except Exception:
            continue
    return cards


def cards_needing_check(tasks_dir: Path, now: datetime | None = None) -> list[TaskCard]:
    """Return pending cards whose trigger_script should be polled now."""
    return [c for c in load_all_cards(tasks_dir) if c.needs_check(now)]


def cards_with_end_check_due(tasks_dir: Path, now: datetime | None = None) -> list[TaskCard]:
    """Return working cards whose end_script should be polled now."""
    out: list[TaskCard] = []
    for c in load_all_cards(tasks_dir):
        if c.end_check_due(now) and end_script_path(tasks_dir, c.name).is_file():
            out.append(c)
    return out


def has_pending_cards(tasks_dir: Path) -> bool:
    """True if any card has status=pending (ready to be checked)."""
    return any(c.status == "pending" for c in load_all_cards(tasks_dir))


def clear_all_cards(tasks_dir: Path) -> None:
    """Mark all cards as finished (used on SESSION_FINISHED)."""
    for card in load_all_cards(tasks_dir):
        card.mark_terminal()
        save_card(tasks_dir, card)


def pause_all_cards(tasks_dir: Path) -> int:
    """Mark every active card (pending/working) as paused. Returns count.

    Paired with ``resume_all_paused_cards`` — used by ``stop_session`` so a
    stopped session also halts its scheduled wakeups. Without this, the
    runtime's pending-card scan would keep firing trigger_scripts even
    while the session sits stopped.
    """
    count = 0
    for card in load_all_cards(tasks_dir):
        if card.status in ("pending", "working"):
            card.mark_paused()
            save_card(tasks_dir, card)
            count += 1
    return count


def resume_all_paused_cards(tasks_dir: Path) -> int:
    """Flip every paused card back to pending. Returns count.

    Symmetric to ``pause_all_cards`` — invoked by ``start_session`` when
    the user resumes. Cards that were ``finished`` stay finished; only
    ``paused`` is touched.
    """
    count = 0
    for card in load_all_cards(tasks_dir):
        if card.status == "paused":
            card.mark_pending()
            save_card(tasks_dir, card)
            count += 1
    return count


def ensure_card(
    tasks_dir: Path,
    name: str,
    *,
    check_interval: float | None = None,
    description: str = "",
    trigger_script: str | None = None,
    end_script: str | None = None,
) -> TaskCard:
    """Ensure a task card (+scripts) exists. Idempotent.

    Returns the existing card unchanged if one is on disk. The trigger
    script defaults to ``echo [start]`` (unconditional fire every
    ``check_interval`` seconds) when not supplied — matches the most
    common "recurring" use case without forcing the caller to write bash.
    """
    tasks_dir.mkdir(parents=True, exist_ok=True)
    existing = load_card(tasks_dir, name)
    if existing is not None:
        return existing
    card = TaskCard(
        name=name,
        description=description,
        check_interval=float(check_interval) if check_interval else _DEFAULT_CHECK_INTERVAL,
        status="pending",
    )
    save_card(tasks_dir, card)
    if not trigger_script_path(tasks_dir, name).exists():
        write_trigger_script(tasks_dir, name, trigger_script or f"echo {_TRIGGER_FIRE}")
    if end_script is not None:
        write_end_script(tasks_dir, name, end_script)
    return card
