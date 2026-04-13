from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from nutshell.session_engine.session_config import read_config
from nutshell.session_engine.session_status import read_session_status, write_session_status, pid_alive as _pid_alive


_SAFE_ID = re.compile(r'^[\w\-]+$')


def _validate_session_id(session_id: str) -> None:
    if not _SAFE_ID.match(session_id):
        raise ValueError(f"Invalid session_id: {session_id!r}")


def is_meta_session(session_id: str) -> bool:
    return session_id.endswith("_meta")


def _is_stale_stopped(info: dict) -> bool:
    if info.get("status") != "stopped":
        return False
    ts = info.get("stopped_at") or info.get("updated_at")
    if not ts:
        return False
    try:
        stopped_at = datetime.fromisoformat(ts)
    except Exception:
        return False
    now = datetime.now(stopped_at.tzinfo) if stopped_at.tzinfo is not None else datetime.now()
    return (now - stopped_at).total_seconds() >= 12 * 3600


def get_session(session_id: str, sessions_dir: Path, system_sessions_dir: Path) -> dict | None:
    _validate_session_id(session_id)
    session_dir = sessions_dir / session_id
    system_dir = system_sessions_dir / session_id
    manifest_path = system_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        manifest = {}
    status_payload = read_session_status(system_dir)
    params = read_config(session_dir) if session_dir.exists() else {}
    from nutshell.session_engine.task_cards import has_pending_cards
    tasks_dir = session_dir / "core" / "tasks"
    has_tasks = has_pending_cards(tasks_dir)
    cards_mtimes = [f.stat().st_mtime for f in list(tasks_dir.glob("*.json")) + list(tasks_dir.glob("*.md"))] if tasks_dir.is_dir() else []
    tasks_mtime = datetime.fromtimestamp(max(cards_mtimes)).isoformat() if cards_mtimes else None
    pid_alive = _pid_alive(status_payload.get("pid"))
    status = status_payload.get("status", "active")
    return {
        "id": session_id,
        "entity": manifest.get("entity", "?"),
        "created_at": manifest.get("created_at", ""),
        "pid_alive": pid_alive,
        "status": status,
        "has_tasks": has_tasks,
        "model_state": status_payload.get("model_state", "idle"),
        "model_source": status_payload.get("model_source"),
        "last_run_at": status_payload.get("last_run_at"),
        "updated_at": status_payload.get("updated_at"),
        "stopped_at": status_payload.get("stopped_at"),
        "tasks_updated_at": tasks_mtime,
        "params": params,
        "alive": pid_alive and status != "stopped",
    }


def _session_priority(info: dict) -> int:
    if info.get("model_state") == "running" and info.get("pid_alive") and info.get("status") != "stopped":
        return 0
    if info.get("has_tasks") and info.get("pid_alive") and info.get("status") != "stopped":
        return 1
    if info.get("status") != "stopped":
        return 2
    return 4 if _is_stale_stopped(info) else 3


def sort_sessions(sessions: list[dict]) -> list[dict]:
    sessions.sort(key=lambda s: s.get("last_run_at") or s.get("created_at") or "", reverse=True)
    sessions.sort(key=_session_priority)
    return sessions


def list_sessions(sessions_dir: Path, system_sessions_dir: Path, exclude_meta: bool = True) -> list[dict]:
    if not system_sessions_dir.is_dir():
        return []
    result = []
    for d in sorted(system_sessions_dir.iterdir()):
        if not d.is_dir():
            continue
        if exclude_meta and is_meta_session(d.name):
            continue
        info = get_session(d.name, sessions_dir, system_sessions_dir)
        if info is not None:
            result.append(info)
    return sort_sessions(result)


def create_session(session_id: str, entity: str, sessions_dir: Path, system_sessions_dir: Path) -> dict:
    """Create a new session."""
    _validate_session_id(session_id)
    from nutshell.session_engine.session_init import init_session
    entity_path = Path(entity)
    if len(entity_path.parts) >= 2 and entity_path.parts[0] == "entity":
        entity_name = str(Path(*entity_path.parts[1:]))
        entity_base = sessions_dir.parent / "entity"
    elif entity_path.is_absolute() or entity_path.parent != Path('.'):
        entity_name = entity_path.name
        entity_base = entity_path.parent.resolve() if not entity_path.is_absolute() else entity_path.parent
    else:
        entity_name = entity
        entity_base = sessions_dir.parent / "entity"
    init_session(
        session_id=session_id,
        entity_name=entity_name,
        sessions_base=sessions_dir,
        system_sessions_base=system_sessions_dir,
        entity_base=entity_base,
    )
    return {"id": session_id, "entity": entity}


def delete_session(session_id: str, sessions_dir: Path, system_sessions_dir: Path) -> bool:
    _validate_session_id(session_id)
    system_dir = system_sessions_dir / session_id
    session_dir = sessions_dir / session_id
    if not system_dir.exists() and not session_dir.exists():
        return False
    write_session_status(system_dir, status="stopped", pid=None, stopped_at=datetime.now().isoformat())
    if session_dir.exists():
        shutil.rmtree(session_dir)
    if system_dir.exists():
        shutil.rmtree(system_dir)
    return True


def stop_session(session_id: str, system_sessions_dir: Path) -> bool:
    _validate_session_id(session_id)
    system_dir = system_sessions_dir / session_id
    if not (system_dir / 'manifest.json').exists():
        return False
    write_session_status(system_dir, status="stopped", pid=None, stopped_at=datetime.now().isoformat())
    from nutshell.runtime.ipc import FileIPC
    FileIPC(system_dir).append_event({"type": "status", "value": "paused — use ▶ Start to resume"})
    return True


def start_session(session_id: str, system_sessions_dir: Path) -> bool:
    _validate_session_id(session_id)
    system_dir = system_sessions_dir / session_id
    if not (system_dir / 'manifest.json').exists():
        return False
    write_session_status(system_dir, status="active", stopped_at=None)
    from nutshell.runtime.ipc import FileIPC
    FileIPC(system_dir).append_event({"type": "status", "value": "resumed"})
    return True
