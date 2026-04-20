"""task_update tool — edit fields on an existing task card.

v2.0.30: the script field switched from full-replace to edit semantics
(``old_string`` + ``new_string``, optional ``replace_all``). This
matches the ``edit`` tool exactly so an agent tuning a script makes the
minimal textual change the UI can diff cleanly, rather than re-pasting
the whole body on every tweak. Metadata fields (description,
check_interval, progress, comments) remain full-replace — they're
single-value strings / numbers where an edit would be overkill.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from butterfly.session_engine.task_cards import (
    load_card,
    read_script,
    save_card,
    write_script,
)


_UNSET = object()


class TaskUpdateExecutor:
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
        description: Any = _UNSET,
        check_interval: Any = _UNSET,
        old_string: Any = _UNSET,
        new_string: Any = _UNSET,
        replace_all: Any = False,
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

        # Script edit — mirrors the `edit` tool's uniqueness + replace_all
        # rules. Either both present or both absent; lone old_string /
        # lone new_string is a usage error.
        script_edit_requested = (old_string is not _UNSET) or (new_string is not _UNSET)
        if script_edit_requested:
            if old_string is _UNSET or new_string is _UNSET:
                return (
                    "Error: pass both 'old_string' and 'new_string' to edit "
                    "the task's script. Lone one-or-the-other is not allowed."
                )
            if not isinstance(old_string, str) or not isinstance(new_string, str):
                return "Error: 'old_string' and 'new_string' must be strings."
            if old_string == "":
                return (
                    "Error: old_string must be non-empty. task_update edits "
                    "the bash script with exact-string replacement."
                )
            if old_string == new_string:
                return "Error: old_string and new_string are identical; no change."
            current = read_script(self._tasks_dir, name) or ""
            count = current.count(old_string)
            if count == 0:
                return f"Error: old_string not found in script of task '{name}'."
            if count > 1 and not replace_all:
                return (
                    f"Error: old_string appears {count} times in the script "
                    f"of task '{name}'. Pass replace_all=true or supply more "
                    f"context."
                )
            if replace_all:
                updated = current.replace(old_string, new_string)
                replacements = count
            else:
                updated = current.replace(old_string, new_string, 1)
                replacements = 1
            write_script(self._tasks_dir, name, updated)
            changed.append(f"script ({replacements} {'replacement' if replacements == 1 else 'replacements'})")

        if not changed:
            return f"Task '{name}': no fields provided to update."

        save_card(self._tasks_dir, card)
        if self._on_change is not None:
            try:
                self._on_change(name, "updated")
            except Exception:  # noqa: BLE001 — refresh hint, best-effort
                pass
        return f"Updated task '{name}' ({', '.join(changed)})."
