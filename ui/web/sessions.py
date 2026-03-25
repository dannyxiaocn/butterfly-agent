"""Session helper functions: read metadata, sort, and initialize."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from nutshell.runtime.status import read_session_status, pid_alive as _pid_alive


def _read_session_info(session_dir: Path, system_dir: Path) -> dict | None:
    """Read session metadata from manifest.json (static) and status.json (dynamic)."""
    manifest_path = system_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        manifest = {}
    status_payload = read_session_status(system_dir)
    tasks_path = session_dir / "core" / "tasks.md"
    has_tasks = tasks_path.exists() and bool(tasks_path.read_text(encoding="utf-8").strip())
    tasks_mtime = (
        datetime.fromtimestamp(tasks_path.stat().st_mtime).isoformat()
        if tasks_path.exists() else None
    )
    pid_alive = _pid_alive(status_payload.get("pid"))
    status = status_payload.get("status", "active")
    return {
        "id": system_dir.name,
        "entity": manifest.get("entity", "?"),
        "created_at": manifest.get("created_at", ""),
        "heartbeat": manifest.get("heartbeat", 10.0),
        "pid_alive": pid_alive,
        "status": status,
        "has_tasks": has_tasks,
        "model_state": status_payload.get("model_state", "idle"),
        "model_source": status_payload.get("model_source"),
        "last_run_at": status_payload.get("last_run_at"),
        "tasks_updated_at": tasks_mtime,
        "heartbeat_interval": status_payload.get("heartbeat_interval", 600.0),
        "alive": pid_alive and status != "stopped",
    }


def _session_priority(info: dict) -> int:
    """Return sort priority: 0=running, 1=napping(tasks queued), 2=stopped, 3=idle."""
    if info.get("model_state") == "running" and info.get("pid_alive") and info.get("status") != "stopped":
        return 0
    if info.get("has_tasks") and info.get("pid_alive") and info.get("status") != "stopped":
        return 1
    if info.get("status") == "stopped":
        return 2
    return 3


def _sort_sessions(sessions: list[dict]) -> list[dict]:
    """Sort sessions: running > queued > idle > stopped, then by most recently run."""
    sessions.sort(key=lambda s: s.get("last_run_at") or s.get("created_at") or "", reverse=True)
    sessions.sort(key=_session_priority)
    return sessions


def _init_session(
    sessions_dir: Path,
    system_sessions_dir: Path,
    session_id: str,
    entity: str,
    heartbeat: float,
) -> None:
    """Initialize a new session directory structure by copying entity content to core/.

    Delegates to nutshell.runtime.session_factory.init_session.
    `entity` may be a full relative path ('entity/agent') or just a name ('agent').
    """
    from nutshell.runtime.session_factory import init_session

    # Resolve entity_name and entity_base from the entity string
    # Web UI historically passes full paths like "entity/agent"
    entity_path = Path(entity)
    if len(entity_path.parts) >= 2 and entity_path.parts[0] == "entity":
        entity_name = str(Path(*entity_path.parts[1:]))
        entity_base = sessions_dir.parent / "entity"
    elif entity_path.is_absolute() or entity_path.parent != Path("."):
        # Full or relative path — use parent as entity_base
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
        heartbeat=heartbeat,
    )
