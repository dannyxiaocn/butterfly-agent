"""task_list tool — list all task cards, optionally filtered by status."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from butterfly.session_engine.task_cards import (
    load_all_cards,
    script_path,
)


class TaskListExecutor:
    def __init__(self, tasks_dir: str | Path | None = None) -> None:
        self._tasks_dir = Path(tasks_dir) if tasks_dir else None

    async def execute(self, status: str | None = None, **_: Any) -> str:
        if self._tasks_dir is None:
            return "Error: tasks directory not configured."
        cards = load_all_cards(self._tasks_dir)
        if status:
            want = str(status).strip().lower()
            cards = [c for c in cards if c.status == want]
        if not cards:
            return "No task cards found." if not status else f"No task cards with status '{status}'."
        lines = []
        for c in cards:
            has_script = "yes" if script_path(self._tasks_dir, c.name).is_file() else "no"
            last = c.last_finished_at or c.last_started_at or "never"
            lines.append(
                f"{c.name} [{c.status}] check_interval={c.check_interval:g}s "
                f"script={has_script} last={last}"
            )
        return "\n".join(lines)
