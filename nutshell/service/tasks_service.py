from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .sessions_service import _validate_session_id


def get_tasks(session_id: str, sessions_dir: Path) -> list[dict]:
    _validate_session_id(session_id)
    from nutshell.session_engine.task_cards import load_all_cards, migrate_legacy_task_sources
    session_dir = sessions_dir / session_id
    if session_dir.exists():
        migrate_legacy_task_sources(session_dir)
    tasks_dir = session_dir / 'core' / 'tasks'
    cards = sorted(load_all_cards(tasks_dir), key=lambda c: (c.name != 'duty', c.name.lower()))
    return [c.to_dict() for c in cards]


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
        if name == 'duty' and interval is None:
            interval = 7200.0  # default interval for duty cards
        # Normalize legacy status values
        _STATUS_MAP = {"running": "working", "completed": "finished"}
        raw_status = task_fields.get('status', existing.status if existing else 'pending')
        status = _STATUS_MAP.get(raw_status, raw_status)

        card = TaskCard(
            name=name,
            description=task_fields.get('description', existing.description if existing else ''),
            interval=interval,
            start_at=task_fields.get('start_at', existing.start_at if existing else None),
            end_at=task_fields.get('end_at', existing.end_at if existing else None),
            status=status,
            last_finished_at=task_fields.get('last_finished_at', existing.last_finished_at if existing else None),
            last_started_at=task_fields.get('last_started_at', existing.last_started_at if existing else None),
            created_at=task_fields.get('created_at', existing.created_at if existing else datetime.now().isoformat()),
            comments=task_fields.get('comments', existing.comments if existing else ''),
            progress=task_fields.get('progress', existing.progress if existing else ''),
        )
        if previous_name != name:
            if load_card(tasks_dir, name) is not None:
                raise FileExistsError(name)
            delete_card(tasks_dir, previous_name)
        save_card(tasks_dir, card)
    elif 'description' in task_fields:
        from nutshell.session_engine.task_cards import TaskCard, save_card
        save_card(tasks_dir, TaskCard(name='task', description=task_fields['description']))
    return True


def delete_task(session_id: str, task_name: str, sessions_dir: Path) -> bool:
    _validate_session_id(session_id)
    from nutshell.session_engine.task_cards import delete_card, migrate_legacy_task_sources
    session_dir = sessions_dir / session_id
    if not session_dir.exists():
        return False
    migrate_legacy_task_sources(session_dir)
    return delete_card(session_dir / 'core' / 'tasks', task_name)
