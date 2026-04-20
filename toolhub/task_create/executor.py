"""task_create tool — bash-driven task card creation (v2.0.29).

The agent writes ONE bash snippet plus a cadence:

    * ``script`` (required) — polled every ``check_interval`` seconds
      while status == ``pending``. The LAST line of stdout decides:
          [skip]             → not yet, check again next interval
          [start]            → wake the agent now
          [start] <message>  → wake the agent; <message> becomes the seed
          [done]             → mark this card finished; never poll again

The script lives at ``core/tasks/<name>.sh`` and the agent can read /
edit it directly with bash. ``[done]`` is the script's way to retire a
card without an agent wakeup; the agent itself can call ``task_finish``
from inside a wakeup to do the same thing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from butterfly.session_engine.task_cards import (
    TaskCard,
    load_card,
    save_card,
    write_script,
)


class TaskCreateExecutor:
    def __init__(
        self,
        tasks_dir: str | Path | None = None,
        on_change: Callable[[str, str], None] | None = None,
    ) -> None:
        self._tasks_dir = Path(tasks_dir) if tasks_dir else None
        self._on_change = on_change

    async def execute(
        self,
        name: str = "",
        description: str = "",
        check_interval: float | None = None,
        script: str = "",
        **_: Any,
    ) -> str:
        if self._tasks_dir is None:
            return "Error: tasks directory not configured."
        name = (name or "").strip()
        if not name:
            return "Error: 'name' is required."
        if not (script or "").strip():
            return (
                "Error: 'script' is required. Write a bash snippet whose "
                "LAST line is `[skip]`, `[start]`, `[start] <message>`, "
                "or `[done]`."
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
            write_script(self._tasks_dir, name, script)
        except ValueError as e:
            return f"Error: {e}"
        if self._on_change is not None:
            try:
                self._on_change(name, "created")
            except Exception:  # noqa: BLE001 — refresh hint, best-effort
                pass
        return (
            f"Created task '{name}' (check_interval={interval:g}s). "
            f"Script: core/tasks/{name}.sh"
        )
