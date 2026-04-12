"""manage_task tool — CRUD operations on task card JSON files."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


class ManageTaskExecutor:
    def __init__(self, tasks_dir: str | Path | None = None) -> None:
        self._tasks_dir = Path(tasks_dir) if tasks_dir else None

    async def execute(self, **kwargs: Any) -> str:
        if self._tasks_dir is None:
            return "Error: tasks directory not configured"

        action = kwargs.get("action", "")
        name = kwargs.get("name", "")

        if action == "list":
            return self._list_tasks()
        if not name:
            return "Error: 'name' is required for create/update/pause/finish"
        if action == "create":
            return self._create_task(name, kwargs)
        elif action == "update":
            return self._update_task(name, kwargs)
        elif action == "pause":
            return self._set_status(name, "paused")
        elif action == "finish":
            return self._set_status(name, "finished")
        return f"Error: unknown action '{action}'"

    def _task_path(self, name: str) -> Path:
        return self._tasks_dir / f"{name}.json"

    def _load_task(self, name: str) -> dict | None:
        path = self._task_path(name)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _save_task(self, name: str, data: dict) -> None:
        self._tasks_dir.mkdir(parents=True, exist_ok=True)
        path = self._task_path(name)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _list_tasks(self) -> str:
        if not self._tasks_dir or not self._tasks_dir.is_dir():
            return "No tasks found."
        tasks = []
        for p in sorted(self._tasks_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                tasks.append(data)
            except Exception:
                continue
        if not tasks:
            return "No tasks found."
        lines = []
        for t in tasks:
            status = t.get("status", "?")
            name = t.get("name", t.get("_filename", "?"))
            desc = t.get("description", "")
            interval = t.get("interval")
            interval_str = f" (every {interval}s)" if interval else " (one-shot)"
            lines.append(f"- {name} [{status}]{interval_str}: {desc}")
        return "\n".join(lines)

    def _create_task(self, name: str, kwargs: dict) -> str:
        if self._load_task(name) is not None:
            return f"Error: task '{name}' already exists. Use 'update' to modify it."
        now = datetime.now().isoformat()
        data = {
            "name": name,
            "description": kwargs.get("description", ""),
            "status": "paused",
            "interval": kwargs.get("interval"),
            "created_at": now,
            "last_started_at": None,
            "last_finished_at": None,
            "comments": kwargs.get("comments", ""),
            "progress": kwargs.get("progress", ""),
        }
        self._save_task(name, data)
        return f"Task '{name}' created."

    def _update_task(self, name: str, kwargs: dict) -> str:
        data = self._load_task(name)
        if data is None:
            return f"Error: task '{name}' not found."
        for field in ("description", "comments", "progress", "interval"):
            if field in kwargs and kwargs[field] is not None:
                data[field] = kwargs[field]
        self._save_task(name, data)
        return f"Task '{name}' updated."

    def _set_status(self, name: str, status: str) -> str:
        data = self._load_task(name)
        if data is None:
            return f"Error: task '{name}' not found."
        data["status"] = status
        if status == "finished":
            data["last_finished_at"] = datetime.now().isoformat()
        self._save_task(name, data)
        return f"Task '{name}' status set to '{status}'."
