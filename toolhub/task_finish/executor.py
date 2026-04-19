"""task_finish tool — mark a task card as finished."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from butterfly.session_engine.task_cards import load_card, save_card


class TaskFinishExecutor:
    def __init__(self, tasks_dir: str | Path | None = None) -> None:
        self._tasks_dir = Path(tasks_dir) if tasks_dir else None

    async def execute(self, name: str = "", **_: Any) -> str:
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
        # v2.0.27: manual terminate must force status=finished regardless of
        # ``interval``. ``TaskCard.mark_finished()`` sets recurring cards
        # back to "pending" (so the dispatcher's next poll re-schedules
        # them for the next interval) — that semantic is for the normal
        # "tick completed" path in ``_do_tick``, not for the agent-invoked
        # terminate verb. Calling ``mark_finished`` here used to leave
        # recurring tasks looping forever.
        card.status = "finished"
        card.last_finished_at = datetime.now().isoformat()
        save_card(self._tasks_dir, card)
        return f"Task '{name}' marked finished."
