"""task_list tool — list all task cards, optionally filtered by status."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from butterfly.session_engine.task_cards import (
    end_script_path,
    load_all_cards,
    trigger_script_path,
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
            scripts = []
            if trigger_script_path(self._tasks_dir, c.name).is_file():
                scripts.append("trigger")
            if end_script_path(self._tasks_dir, c.name).is_file():
                scripts.append("end")
            scripts_str = ",".join(scripts) if scripts else "-"
            last = c.last_finished_at or c.last_started_at or "never"
            lines.append(
                f"{c.name} [{c.status}] check_interval={c.check_interval:g}s "
                f"scripts={scripts_str} last={last}"
            )
        return "\n".join(lines)
