"""Serve the session's todo list to the web UI.

Companion to ``tasks_service`` — exposes ``core/todo_list.json`` on the
``GET /api/sessions/<id>/todo_list`` route. Returns ``None`` when no
list has been created yet so the frontend can hide the pinned header.
"""
from __future__ import annotations

from pathlib import Path

from butterfly.session_engine.todo_list import (
    format_todo_progress,
    is_todo_all_done,
    load_todo_list,
    todo_active_index,
    todo_pending_count,
)
from .sessions_service import _validate_session_id


def get_todo_list(session_id: str, sessions_dir: Path) -> dict | None:
    """Return a frontend-friendly snapshot of the session's todo list.

    Shape matches the HUD's ``todo`` payload plus the full ``progress``
    + ``comments`` strings (the pinned Tasks header renders both):

        {
          "progress_line": "[2/3] Implementing Y",
          "progress": "[2/3] Implementing Y",
          "comments": "☑ 1. Investigate X",
          "active_index": 2,
          "total": 3,
          "pending_count": 2,
          "all_done": false,
          "iters_since_seen": 2,
          "threshold": 10,
          "updated_at": "2026-04-23T09:12:34",
          "items": [ {content, status, activeForm}, ... ]
        }

    Returns ``None`` when ``core/todo_list.json`` is missing, unreadable,
    or carries an empty ``todos`` array.
    """
    _validate_session_id(session_id)
    core_dir = sessions_dir / session_id / 'core'
    if not core_dir.is_dir():
        return None
    todo = load_todo_list(core_dir)
    if todo is None or not todo.todos:
        return None
    return {
        'progress_line': format_todo_progress(todo.todos),
        'progress': todo.progress,
        'comments': todo.comments,
        'active_index': todo_active_index(todo.todos),
        'total': len(todo.todos),
        'pending_count': todo_pending_count(todo.todos),
        'all_done': is_todo_all_done(todo.todos),
        'iters_since_seen': todo.iters_since_seen,
        'threshold': todo.reminder_threshold,
        'updated_at': todo.updated_at,
        'items': [
            {
                'content': t['content'],
                'status': t['status'],
                'activeForm': t['activeForm'],
            }
            for t in todo.todos
        ],
    }
