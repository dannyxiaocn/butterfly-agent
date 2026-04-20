"""task_resume tool — return a paused task card to pending."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from butterfly.session_engine.task_cards import load_card, save_card


class TaskResumeExecutor:
    def __init__(
        self,
        tasks_dir: str | Path | None = None,
        on_change: Callable[[str, str], None] | None = None,
    ) -> None:
        self._tasks_dir = Path(tasks_dir) if tasks_dir else None
        self._on_change = on_change

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
        card.mark_pending()
        save_card(self._tasks_dir, card)
        if self._on_change is not None:
            try:
                self._on_change(name, "resumed")
            except Exception:  # noqa: BLE001 — refresh hint, best-effort
                pass
        return f"Task '{name}' resumed."
