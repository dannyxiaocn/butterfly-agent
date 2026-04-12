from __future__ import annotations

from pathlib import Path

from nutshell.session_engine.session_config import read_config, write_config
from .sessions_service import _validate_session_id, is_meta_session


def get_config(session_id: str, sessions_dir: Path, system_sessions_dir: Path) -> dict:
    _validate_session_id(session_id)
    session_dir = sessions_dir / session_id
    system_dir = system_sessions_dir / session_id
    if not system_dir.exists() or not session_dir.exists():
        raise FileNotFoundError(session_id)
    cfg = read_config(session_dir)
    return {**cfg, 'is_meta_session': is_meta_session(session_id)}


def update_config(session_id: str, sessions_dir: Path, system_sessions_dir: Path, params: dict) -> dict:
    _validate_session_id(session_id)
    from nutshell.session_engine.task_cards import ensure_card, load_card, migrate_legacy_task_sources, save_card
    session_dir = sessions_dir / session_id
    system_dir = system_sessions_dir / session_id
    if not system_dir.exists() or not session_dir.exists():
        raise FileNotFoundError(session_id)
    params = dict(params)
    params.pop('is_meta_session', None)
    migrate_legacy_task_sources(session_dir)

    # Legacy compat: convert default_task/heartbeat_interval to duty format
    legacy_task = params.pop('default_task', None)
    legacy_interval = params.pop('heartbeat_interval', None)
    if legacy_task or legacy_interval:
        tasks_dir = session_dir / 'core' / 'tasks'
        existing = load_card(tasks_dir, 'duty') or load_card(tasks_dir, 'heartbeat')
        if existing is not None:
            if legacy_task:
                existing.description = str(legacy_task)
            if legacy_interval is not None:
                existing.interval = float(legacy_interval)
            save_card(tasks_dir, existing)
        elif legacy_interval is not None:
            ensure_card(tasks_dir, name='duty', interval=float(legacy_interval), description=str(legacy_task or ''))

    # Sync duty config field with task card
    duty = params.get('duty')
    if isinstance(duty, dict) and duty.get('interval'):
        tasks_dir = session_dir / 'core' / 'tasks'
        existing = load_card(tasks_dir, 'duty')
        if existing is not None:
            existing.interval = float(duty['interval'])
            existing.description = duty.get('description', existing.description)
            save_card(tasks_dir, existing)
        else:
            ensure_card(tasks_dir, name='duty', interval=float(duty['interval']), description=duty.get('description', ''))

    write_config(session_dir, **params)
    saved = read_config(session_dir)
    return {**saved, 'is_meta_session': is_meta_session(session_id)}
