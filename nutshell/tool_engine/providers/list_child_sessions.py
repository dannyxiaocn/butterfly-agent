from __future__ import annotations

import json
import os
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_DEFAULT_SESSIONS_BASE = _REPO_ROOT / "_sessions"
_DEFAULT_SYSTEM_BASE = _REPO_ROOT / "_sessions"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _count_tasks(tasks_text: str) -> int:
    return sum(1 for line in tasks_text.splitlines() if line.strip())


async def list_child_sessions(*, _entity=None, _sessions_base=None, _system_base=None) -> str:
    entity = _entity or os.environ.get("NUTSHELL_ENTITY")
    if not entity:
        return "[]"

    sessions_base = Path(_sessions_base) if _sessions_base is not None else _DEFAULT_SESSIONS_BASE
    system_base = Path(_system_base) if _system_base is not None else _DEFAULT_SYSTEM_BASE
    archived_system_base = system_base.parent / "_archived" / system_base.name

    archived_ids: set[str] = set()
    if archived_system_base.is_dir():
        archived_ids = {p.name for p in archived_system_base.iterdir() if p.is_dir()}

    rows: list[dict] = []
    if sessions_base.is_dir():
        for sess_dir in sorted(p for p in sessions_base.iterdir() if p.is_dir()):
            session_id = sess_dir.name
            if session_id.endswith("_meta") or session_id in archived_ids:
                continue
            manifest = _read_json(sess_dir / "manifest.json")
            if manifest.get("entity") != entity:
                continue
            status = _read_json(sess_dir / "status.json")
            tasks_path = system_base.parent / "sessions" / session_id / "core" / "tasks.md"
            tasks_text = tasks_path.read_text(encoding="utf-8") if tasks_path.exists() else ""
            rows.append({
                "session_id": session_id,
                "status": status.get("status"),
                "last_run": status.get("last_run_at"),
                "task_count": _count_tasks(tasks_text),
            })

    return json.dumps(rows, ensure_ascii=False)
