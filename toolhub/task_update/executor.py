"""task_update tool — update selected fields on an existing task card."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from butterfly.session_engine.task_cards import (
    load_card,
    save_card,
    write_end_script,
    write_trigger_script,
)


_UNSET = object()


class TaskUpdateExecutor:
    def __init__(self, tasks_dir: str | Path | None = None) -> None:
        self._tasks_dir = Path(tasks_dir) if tasks_dir else None

    async def execute(
        self,
        name: str = "",
        description: Any = _UNSET,
        check_interval: Any = _UNSET,
        trigger_script: Any = _UNSET,
        end_script: Any = _UNSET,
        progress: Any = _UNSET,
        comments: Any = _UNSET,
        **_: Any,
    ) -> str:
        if self._tasks_dir is None:
            return "Error: tasks directory not configured."
        name = (name or "").strip()
        if not name:
            return "Error: 'name' is required."

        try:
            card = load_card(self._tasks_dir, name)
        except ValueError as e:
            return f"Error: {e}"
        if card is None:
            return f"Error: Task '{name}' not found."

        changed: list[str] = []
        if description is not _UNSET:
            card.description = description or ""
            changed.append("description")
        if check_interval is not _UNSET and check_interval is not None:
            try:
                new_interval = float(check_interval)
            except (TypeError, ValueError):
                return "Error: 'check_interval' must be a number."
            if new_interval <= 0:
                return "Error: 'check_interval' must be > 0."
            card.check_interval = new_interval
            changed.append("check_interval")
        if progress is not _UNSET:
            card.progress = progress or ""
            changed.append("progress")
        if comments is not _UNSET:
            card.comments = comments or ""
            changed.append("comments")
        if trigger_script is not _UNSET and trigger_script is not None:
            write_trigger_script(self._tasks_dir, name, str(trigger_script))
            changed.append("trigger_script")
        if end_script is not _UNSET:
            write_end_script(self._tasks_dir, name, end_script)
            changed.append("end_script")

        if not changed:
            return f"Task '{name}': no fields provided to update."

        save_card(self._tasks_dir, card)
        return f"Updated task '{name}' ({', '.join(changed)})."
