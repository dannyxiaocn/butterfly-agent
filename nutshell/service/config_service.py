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
    from nutshell.session_engine.task_cards import ensure_heartbeat_card, load_card, migrate_legacy_task_sources, save_card
    session_dir = sessions_dir / session_id
    system_dir = system_sessions_dir / session_id
    if not system_dir.exists() or not session_dir.exists():
        raise FileNotFoundError(session_id)
    params = dict(params)
    params.pop('is_meta_session', None)
    migrate_legacy_task_sources(session_dir)
    if 'default_task' in params:
        heartbeat_content = params.pop('default_task')
        if heartbeat_content not in (None, ''):
            existing_heartbeat = load_card(session_dir / 'core' / 'tasks', 'heartbeat')
            if existing_heartbeat is None:
                ensure_heartbeat_card(
                    session_dir / 'core' / 'tasks',
                    interval=float(params.get('heartbeat_interval') or read_config(session_dir).get('heartbeat_interval') or 7200.0),
                    content=str(heartbeat_content),
                )
            else:
                existing_heartbeat.content = str(heartbeat_content)
                save_card(session_dir / 'core' / 'tasks', existing_heartbeat)
    if 'heartbeat_interval' in params:
        interval = params['heartbeat_interval']
        if interval is not None:
            heartbeat = load_card(session_dir / 'core' / 'tasks', 'heartbeat')
            if heartbeat is not None:
                heartbeat.interval = interval
                save_card(session_dir / 'core' / 'tasks', heartbeat)
            elif params.get('session_type') == 'persistent':
                ensure_heartbeat_card(session_dir / 'core' / 'tasks', interval=interval)
    write_config(session_dir, **params)
    saved = read_config(session_dir)
    return {**saved, 'is_meta_session': is_meta_session(session_id)}
