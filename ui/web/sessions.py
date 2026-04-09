"""Session helper functions: read metadata, sort, and initialize."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from nutshell.session_engine.session_params import read_session_params
from nutshell.session_engine.session_status import read_session_status, pid_alive as _pid_alive


def _is_meta_session_id(session_id: str) -> bool:
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


def _read_session_info(session_dir: Path, system_dir: Path) -> dict | None:
    """Read session metadata from manifest.json (static) and status.json (dynamic)."""
    manifest_path = system_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        manifest = {}
    if session_dir.exists():
        from nutshell.session_engine.task_cards import migrate_legacy_task_sources
        migrate_legacy_task_sources(session_dir)
    status_payload = read_session_status(system_dir)
    params = read_session_params(session_dir) if session_dir.exists() else {}
    from nutshell.session_engine.task_cards import has_pending_cards, load_all_cards
    tasks_dir = session_dir / "core" / "tasks"
    has_tasks = has_pending_cards(tasks_dir)
    # Use latest card mtime for UI freshness indicator
    cards_mtimes = [f.stat().st_mtime for f in tasks_dir.glob("*.md")] if tasks_dir.is_dir() else []
    tasks_mtime = (
        datetime.fromtimestamp(max(cards_mtimes)).isoformat()
        if cards_mtimes else None
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
        "updated_at": status_payload.get("updated_at"),
        "stopped_at": status_payload.get("stopped_at"),
        "tasks_updated_at": tasks_mtime,
        "heartbeat_interval": status_payload.get("heartbeat_interval", 600.0),
        "session_type": params.get("session_type", "default"),
        "params": params,
        "alive": pid_alive and status != "stopped",
    }


def _session_priority(info: dict) -> int:
    """Return sort priority: 0=running, 1=napping(tasks queued), 2=fresh stopped, 3=idle/stale stopped."""
    if info.get("model_state") == "running" and info.get("pid_alive") and info.get("status") != "stopped":
        return 0
    if info.get("has_tasks") and info.get("pid_alive") and info.get("status") != "stopped":
        return 1
    if info.get("status") == "stopped":
        if _is_stale_stopped(info):
            return 3
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

    Delegates to nutshell.session_engine.session_init.init_session.
    `entity` may be a full relative path ('entity/agent') or just a name ('agent').
    """
    from nutshell.session_engine.session_init import init_session

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
