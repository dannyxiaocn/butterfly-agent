"""Todo-list — session-scoped checklist decoupled from task cards.

Storage: ``core/todo_list.json`` (NOT under ``core/tasks/``). The agent
interacts with it via the ``todo_list`` tool; the runtime injects a
re-reminder ChatItem when ``iters_since_seen`` crosses
``reminder_threshold`` so the agent keeps the list in view across long
multi-turn work. Mirrors Claude Code's V1 ``TodoWrite`` semantics.

The todo list is deliberately NOT a task card: ``task_*`` tools operate
on ``core/tasks/`` and see none of this, so the agent cannot tamper with
its own todo list through those tools. The script-polled "trigger"
mechanism from earlier iterations is gone — the reminder is a direct
runtime enqueue, not a bash subprocess.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


TODO_LIST_FILENAME = "todo_list.json"
TODO_DEFAULT_THRESHOLD = 10
TODO_STATUSES = ("pending", "in_progress", "completed")


@dataclass
class TodoList:
    """Session todo list state persisted to ``core/todo_list.json``."""
    todos: list[dict] = field(default_factory=list)
    iters_since_seen: int = 0
    reminder_threshold: int = TODO_DEFAULT_THRESHOLD
    progress: str = ""
    comments: str = ""
    updated_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "todos": list(self.todos),
            "iters_since_seen": int(self.iters_since_seen),
            "reminder_threshold": int(self.reminder_threshold),
            "progress": self.progress,
            "comments": self.comments,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TodoList":
        raw_todos = data.get("todos")
        todos = (
            [t for t in raw_todos if isinstance(t, dict)]
            if isinstance(raw_todos, list)
            else []
        )
        try:
            threshold = int(data.get("reminder_threshold", TODO_DEFAULT_THRESHOLD))
            if threshold < 1:
                threshold = TODO_DEFAULT_THRESHOLD
        except (TypeError, ValueError):
            threshold = TODO_DEFAULT_THRESHOLD
        try:
            iters = int(data.get("iters_since_seen", 0))
            if iters < 0:
                iters = 0
        except (TypeError, ValueError):
            iters = 0
        return cls(
            todos=todos,
            iters_since_seen=iters,
            reminder_threshold=threshold,
            progress=str(data.get("progress") or ""),
            comments=str(data.get("comments") or ""),
            updated_at=data.get("updated_at"),
        )


# ── File IO ───────────────────────────────────────────────────────────────────

def _todo_path(core_dir: Path) -> Path:
    return core_dir / TODO_LIST_FILENAME


def load_todo_list(core_dir: Path) -> TodoList | None:
    """Return the session's todo list, or None when absent / unreadable."""
    path = _todo_path(core_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return TodoList.from_dict(data)


def save_todo_list(core_dir: Path, todo: TodoList) -> Path:
    """Write the todo list to disk (creates ``core/`` if missing)."""
    core_dir.mkdir(parents=True, exist_ok=True)
    todo.updated_at = datetime.now().isoformat()
    path = _todo_path(core_dir)
    path.write_text(
        json.dumps(todo.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def delete_todo_list(core_dir: Path) -> bool:
    """Remove the todo list file; True when something was deleted."""
    path = _todo_path(core_dir)
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


# ── Item helpers ──────────────────────────────────────────────────────────────

def _normalise_todo_status(raw: object) -> str:
    s = str(raw or "pending").strip().lower()
    return s if s in TODO_STATUSES else "pending"


def normalise_todo_items(raw_items: object) -> list[dict]:
    """Coerce caller input into canonical ``{content, status, activeForm}`` items.

    Dropped: non-dict entries, entries whose content is empty after
    trimming. Unknown status → pending. activeForm defaults to content
    when missing. Whitespace around content / activeForm is trimmed.
    """
    if not isinstance(raw_items, list):
        return []
    out: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        active = str(item.get("activeForm", "")).strip() or content
        out.append({
            "content": content,
            "status": _normalise_todo_status(item.get("status")),
            "activeForm": active,
        })
    return out


def todo_active_index(todos: list[dict]) -> int:
    """1-based index of the "current" todo.

    Resolution order: first ``in_progress``; then first ``pending``;
    fallback to ``len(todos)`` (all-done case).
    """
    if not todos:
        return 0
    for i, t in enumerate(todos, 1):
        if t.get("status") == "in_progress":
            return i
    for i, t in enumerate(todos, 1):
        if t.get("status") == "pending":
            return i
    return len(todos)


def todo_pending_count(todos: list[dict]) -> int:
    """Items whose status is not ``completed``."""
    return sum(1 for t in todos if t.get("status") != "completed")


def is_todo_all_done(todos: list[dict]) -> bool:
    """True when the list is non-empty and every item is completed."""
    if not todos:
        return False
    return all(t.get("status") == "completed" for t in todos)


def format_todo_progress(todos: list[dict]) -> str:
    """Render the one-line summary shown on the HUD / Tasks pinned header.

    * Empty list → ``""``.
    * All done   → ``[N/N] <last content>`` so the viewer still sees which
                   checklist just closed; the UI paints the whole line
                   strikethrough + dim.
    * Otherwise  → ``[i/N] <activeForm>`` where ``i`` = active index.
    """
    if not todos:
        return ""
    total = len(todos)
    if is_todo_all_done(todos):
        last = todos[-1]
        label = str(last.get("content") or "").strip()
        return f"[{total}/{total}] {label}" if label else f"[{total}/{total}]"
    idx = todo_active_index(todos)
    if 1 <= idx <= total:
        active = todos[idx - 1]
        label = str(active.get("activeForm") or active.get("content") or "").strip()
        return f"[{idx}/{total}] {label}" if label else f"[{idx}/{total}]"
    return ""


def diff_newly_completed(prev: list[dict] | None, new: list[dict]) -> list[dict]:
    """Items in ``new`` that flipped to ``completed`` since ``prev``.

    Matches by exact ``content`` equality (simple V1 semantics — a
    rename is treated as a new item; usually fine).
    """
    prev_completed: set[str] = set()
    for t in prev or []:
        if t.get("status") == "completed":
            prev_completed.add(str(t.get("content", "")))
    out: list[dict] = []
    for t in new:
        if t.get("status") != "completed":
            continue
        if str(t.get("content", "")) in prev_completed:
            continue
        out.append(t)
    return out


def format_completed_comment_line(index_in_list: int, todo: dict) -> str:
    """One append line for the todo list's ``comments`` log: ``☑ i. content``."""
    content = str(todo.get("content", "")).strip()
    return f"☑ {index_in_list}. {content}"


def format_todo_system_reminder(todos: list[dict]) -> str:
    """Seed text for the runtime's re-reminder.

    Wrapped in ``<system-reminder>`` so the block reads as an
    infrastructure-level hint rather than a user instruction. Emulates
    Claude Code's ``todo_reminder`` attachment.
    """
    if not todos:
        return ""
    lines: list[str] = []
    for i, t in enumerate(todos, 1):
        status = t.get("status", "pending")
        content = t.get("content", "")
        if status == "in_progress":
            active = t.get("activeForm") or content
            lines.append(f"{i}. [in_progress] {content}  (active: {active})")
        else:
            lines.append(f"{i}. [{status}] {content}")
    body = "\n".join(lines)
    return (
        "<system-reminder>\n"
        "Here is the current todo list. Continue working towards pending "
        "and in_progress items; call `todo_list` to update statuses as "
        "you make progress. If every item is already completed, tell the "
        "user. Do NOT mention this reminder to the user.\n\n"
        f"{body}\n"
        "</system-reminder>"
    )
