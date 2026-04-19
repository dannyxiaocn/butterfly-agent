from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .sessions_service import _validate_session_id


def get_tasks(session_id: str, sessions_dir: Path) -> list[dict]:
    _validate_session_id(session_id)
    from butterfly.session_engine.task_cards import (
        load_all_cards,
        read_end_script,
        read_trigger_script,
    )
    session_dir = sessions_dir / session_id
    tasks_dir = session_dir / 'core' / 'tasks'
    cards = sorted(load_all_cards(tasks_dir), key=lambda c: (c.name != 'duty', c.name.lower()))
    out: list[dict] = []
    for c in cards:
        d = c.to_dict()
        d['trigger_script'] = read_trigger_script(tasks_dir, c.name)
        d['end_script'] = read_end_script(tasks_dir, c.name)
        out.append(d)
    return out


def upsert_task(session_id: str, sessions_dir: Path, **task_fields) -> bool:
    _validate_session_id(session_id)
    from butterfly.session_engine.task_cards import (
        TaskCard,
        delete_card,
        load_card,
        save_card,
        write_end_script,
        write_trigger_script,
    )
    session_dir = sessions_dir / session_id
    if not session_dir.exists():
        return False
    tasks_dir = session_dir / 'core' / 'tasks'
    tasks_dir.mkdir(parents=True, exist_ok=True)
    if 'name' in task_fields:
        name = task_fields['name']
        previous_name = task_fields.get('previous_name') or name
        existing = load_card(tasks_dir, previous_name)
        check_interval = task_fields.get(
            'check_interval',
            existing.check_interval if existing else None,
        )
        if check_interval is None:
            check_interval = 7200.0 if name == 'duty' else 3600.0
        status = task_fields.get('status', existing.status if existing else 'pending')

        card = TaskCard(
            name=name,
            description=task_fields.get('description', existing.description if existing else ''),
            check_interval=float(check_interval),
            status=status,
            last_checked_at=task_fields.get('last_checked_at', existing.last_checked_at if existing else None),
            last_started_at=task_fields.get('last_started_at', existing.last_started_at if existing else None),
            last_finished_at=task_fields.get('last_finished_at', existing.last_finished_at if existing else None),
            created_at=task_fields.get('created_at', existing.created_at if existing else datetime.now().isoformat()),
            comments=task_fields.get('comments', existing.comments if existing else ''),
            progress=task_fields.get('progress', existing.progress if existing else ''),
        )
        if previous_name != name:
            if load_card(tasks_dir, name) is not None:
                raise FileExistsError(name)
            delete_card(tasks_dir, previous_name)
        save_card(tasks_dir, card)
        if 'trigger_script' in task_fields and task_fields['trigger_script'] is not None:
            write_trigger_script(tasks_dir, name, task_fields['trigger_script'])
        if 'end_script' in task_fields:
            write_end_script(tasks_dir, name, task_fields['end_script'])
    elif 'description' in task_fields:
        save_card(tasks_dir, TaskCard(name='task', description=task_fields['description']))
    return True


def delete_task(session_id: str, task_name: str, sessions_dir: Path) -> bool:
    _validate_session_id(session_id)
    from butterfly.session_engine.task_cards import delete_card
    session_dir = sessions_dir / session_id
    if not session_dir.exists():
        return False
    return delete_card(session_dir / 'core' / 'tasks', task_name)
