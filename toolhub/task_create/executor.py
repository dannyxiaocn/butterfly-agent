"""task_create tool — bash-driven task card creation (v2.0.27).

The agent writes two bash snippets and a cadence:

    * ``trigger_script`` (required) — polled every ``check_interval`` seconds.
      Last line of stdout decides behaviour:
          [start]            → activate me now
          [start] <message>  → activate me; <message> becomes the seed input
          [skip]             → not yet, check again
    * ``end_script`` (optional) — polled while the agent is running the card.
          [done]     → mark card finished
          [not_done] → keep running

Scripts land on disk at ``core/tasks/<name>.trigger.sh`` and
``core/tasks/<name>.end.sh`` so the agent can read/edit them with bash.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from butterfly.session_engine.task_cards import (
    TaskCard,
    load_card,
    save_card,
    write_end_script,
    write_trigger_script,
)


class TaskCreateExecutor:
    def __init__(self, tasks_dir: str | Path | None = None) -> None:
        self._tasks_dir = Path(tasks_dir) if tasks_dir else None

    async def execute(
        self,
        name: str = "",
        description: str = "",
        check_interval: float | None = None,
        trigger_script: str = "",
        end_script: str | None = None,
        **_: Any,
    ) -> str:
        if self._tasks_dir is None:
            return "Error: tasks directory not configured."
        name = (name or "").strip()
        if not name:
            return "Error: 'name' is required."
        if not (trigger_script or "").strip():
            return (
                "Error: 'trigger_script' is required. Write a bash snippet whose "
                "LAST line is `[start]`, `[start] <message>`, or `[skip]`."
            )
        if load_card(self._tasks_dir, name) is not None:
            return f"Error: Task '{name}' already exists."
        interval = float(check_interval) if check_interval else 3600.0
        if interval <= 0:
            return "Error: 'check_interval' must be > 0."
        try:
            card = TaskCard(
                name=name,
                description=description or "",
                status="pending",
                check_interval=interval,
            )
            save_card(self._tasks_dir, card)
            write_trigger_script(self._tasks_dir, name, trigger_script)
            if end_script is not None:
                write_end_script(self._tasks_dir, name, end_script)
        except ValueError as e:
            return f"Error: {e}"
        return (
            f"Created task '{name}' (check_interval={interval:g}s). "
            f"Trigger script: core/tasks/{name}.trigger.sh"
        )
