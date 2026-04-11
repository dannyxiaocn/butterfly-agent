from __future__ import annotations

from datetime import datetime
from pathlib import Path

from nutshell.session_engine.session_params import read_session_params, write_session_params
from .sessions_service import _validate_session_id


def get_tasks(session_id: str, sessions_dir: Path) -> list[dict]:
    _validate_session_id(session_id)
    from nutshell.session_engine.task_cards import load_all_cards, migrate_legacy_task_sources
    session_dir = sessions_dir / session_id
    if session_dir.exists():
        migrate_legacy_task_sources(session_dir)
    tasks_dir = session_dir / 'core' / 'tasks'
    cards = sorted(load_all_cards(tasks_dir), key=lambda c: (c.name != 'heartbeat', c.name.lower()))
    return [{
        'name': c.name, 'content': c.content, 'interval': c.interval, 'starts_at': c.starts_at,
        'ends_at': c.ends_at, 'status': c.status, 'last_run_at': c.last_run_at, 'created_at': c.created_at,
    } for c in cards]


def upsert_task(session_id: str, sessions_dir: Path, **task_fields) -> bool:
    _validate_session_id(session_id)
    from nutshell.session_engine.task_cards import TaskCard, delete_card, load_card, migrate_legacy_task_sources, save_card
    session_dir = sessions_dir / session_id
    if not session_dir.exists():
        return False
    migrate_legacy_task_sources(session_dir)
    tasks_dir = session_dir / 'core' / 'tasks'
    tasks_dir.mkdir(parents=True, exist_ok=True)
    if 'name' in task_fields:
        name = task_fields['name']
        previous_name = task_fields.get('previous_name') or name
        existing = load_card(tasks_dir, previous_name)
        interval = task_fields.get('interval', existing.interval if existing else None)
        starts_at = task_fields.get('starts_at', existing.starts_at if existing else None)
        ends_at = task_fields.get('ends_at', existing.ends_at if existing else None)
        if name == 'heartbeat' and interval is None:
            interval = float(read_session_params(session_dir).get('heartbeat_interval') or 7200.0)
        card = TaskCard(
            name=name,
            content=task_fields.get('content', existing.content if existing else ''),
            interval=interval,
            starts_at=starts_at,
            ends_at=ends_at,
            status=task_fields.get('status', existing.status if existing else 'pending'),
            last_run_at=task_fields.get('last_run_at', existing.last_run_at if existing else None),
            created_at=task_fields.get('created_at', existing.created_at if existing else datetime.now().isoformat()),
        )
        if previous_name != name:
            if load_card(tasks_dir, name) is not None:
                raise FileExistsError(name)
            delete_card(tasks_dir, previous_name)
        save_card(tasks_dir, card)
        if name == 'heartbeat' and card.interval is not None:
            write_session_params(session_dir, heartbeat_interval=card.interval, default_task=None)
    elif 'content' in task_fields:
        from nutshell.session_engine.task_cards import TaskCard, save_card
        save_card(tasks_dir, TaskCard(name='task', content=task_fields['content']))
    return True


def delete_task(session_id: str, task_name: str, sessions_dir: Path) -> bool:
    _validate_session_id(session_id)
    from nutshell.session_engine.task_cards import delete_card, migrate_legacy_task_sources
    session_dir = sessions_dir / session_id
    if not session_dir.exists():
        return False
    migrate_legacy_task_sources(session_dir)
    return delete_card(session_dir / 'core' / 'tasks', task_name)
