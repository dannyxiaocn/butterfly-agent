"""todo_list tool — whole-list replacement of the session's todo list.

Writes to ``core/todo_list.json``; has no relationship to the task-card
system (``core/tasks/``). The agent rewrites the list with every call —
there is no incremental update. Mirrors Claude Code's V1 ``TodoWrite``.

Side effects on each call:

* ``todos`` replaced wholesale;
* ``iters_since_seen`` reset to 0 (agent just saw the list in full);
* ``progress`` set to ``format_todo_progress(items)`` — one-line
  summary surfaced on the HUD + Tasks-tab pinned header;
* for every todo that newly flipped to ``completed``, one
  ``☑ i. <content>`` line is appended to ``comments`` (log-of-completions);
* the ``on_change`` callback fires so the runtime can emit a
  ``todo_list_changed`` SSE event for the frontend's on-event refresh.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from butterfly.session_engine.todo_list import (
    TodoList,
    diff_newly_completed,
    format_completed_comment_line,
    format_todo_progress,
    load_todo_list,
    normalise_todo_items,
    save_todo_list,
)


_TOOL_RESULT_TEXT = (
    "Todos have been modified successfully. Ensure that you continue to "
    "use the todo list to track your progress. Please proceed with the "
    "current tasks if applicable."
)


class TodoListExecutor:
    def __init__(
        self,
        core_dir: str | Path | None = None,
        on_change: Callable[[str], None] | None = None,
    ) -> None:
        self._core_dir = Path(core_dir) if core_dir else None
        self._on_change = on_change

    async def execute(self, todos: Any = None, **_: Any) -> str:
        if self._core_dir is None:
            return "Error: core directory not configured."

        # ``todos`` may be a non-list (caller forgot the field) —
        # normalise_todo_items drops junk + returns []. A genuine empty
        # list is a valid "clear the list" signal.
        items = normalise_todo_items(todos)

        current = load_todo_list(self._core_dir) or TodoList()
        prev_todos = current.todos or []
        current.todos = items
        current.iters_since_seen = 0
        current.progress = format_todo_progress(items)

        # Append one ☑ line per todo that newly flipped to completed.
        newly_completed = diff_newly_completed(prev_todos, items)
        if newly_completed:
            new_index_by_content: dict[str, int] = {}
            for i, t in enumerate(items, 1):
                content = str(t.get("content", ""))
                # First-wins when the agent somehow lists the same content
                # twice — the earlier position is the one that flipped.
                if content and content not in new_index_by_content:
                    new_index_by_content[content] = i
            appended: list[str] = []
            for todo in newly_completed:
                content = str(todo.get("content", ""))
                idx = new_index_by_content.get(content)
                if idx is None:
                    continue
                appended.append(format_completed_comment_line(idx, todo))
            if appended:
                base = (current.comments or "").rstrip()
                joined = "\n".join(appended)
                current.comments = f"{base}\n{joined}" if base else joined

        save_todo_list(self._core_dir, current)

        if self._on_change is not None:
            try:
                self._on_change("updated")
            except Exception:  # noqa: BLE001 — refresh hint is best-effort
                pass

        return _TOOL_RESULT_TEXT
