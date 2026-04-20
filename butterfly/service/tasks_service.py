from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .sessions_service import _validate_session_id


def _emit_task_card_changed(
    session_id: str,
    card_name: str,
    change: str,
    system_sessions_dir: Path | None = None,
) -> None:
    """Append a `task_card_changed` event to the session's events.jsonl.

    Drives the frontend's on-event Tasks tab refresh (v2.0.30). Silent
    no-op if ``system_sessions_dir`` is missing — we'd rather drop the
    event than crash the API call that triggered it.
    """
    if system_sessions_dir is None:
        return
    system_dir = system_sessions_dir / session_id
    if not system_dir.exists():
        return
    from butterfly.runtime.ipc import FileIPC
    try:
        FileIPC(system_dir).append_event({
            "type": "task_card_changed",
            "card": card_name,
            "change": change,
        })
    except OSError:
        # Non-fatal — the card itself is already persisted; the refresh
        # event is just a performance hint.
        pass


def get_tasks(session_id: str, sessions_dir: Path) -> list[dict]:
    _validate_session_id(session_id)
    from butterfly.session_engine.task_cards import (
        load_all_cards,
        read_script,
    )
    session_dir = sessions_dir / session_id
    tasks_dir = session_dir / 'core' / 'tasks'
    cards = sorted(load_all_cards(tasks_dir), key=lambda c: (c.name != 'duty', c.name.lower()))
    out: list[dict] = []
    for c in cards:
        d = c.to_dict()
        d['script'] = read_script(tasks_dir, c.name)
        out.append(d)
    return out


def upsert_task(
    session_id: str,
    sessions_dir: Path,
    system_sessions_dir: Path | None = None,
    **task_fields,
) -> bool:
    _validate_session_id(session_id)
    from butterfly.session_engine.task_cards import (
        TaskCard,
        delete_card,
        load_card,
        save_card,
        write_script,
    )
    session_dir = sessions_dir / session_id
    if not session_dir.exists():
        return False
    tasks_dir = session_dir / 'core' / 'tasks'
    tasks_dir.mkdir(parents=True, exist_ok=True)
    card_name_for_event: str | None = None
    change_kind: str = "updated"
    if 'name' in task_fields:
        name = task_fields['name']
        previous_name = task_fields.get('previous_name') or name
        existing = load_card(tasks_dir, previous_name)
        change_kind = "created" if existing is None else "updated"
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
        if 'script' in task_fields and task_fields['script'] is not None:
            write_script(tasks_dir, name, task_fields['script'])
        card_name_for_event = name
    elif 'description' in task_fields:
        save_card(tasks_dir, TaskCard(name='task', description=task_fields['description']))
        card_name_for_event = 'task'
    if card_name_for_event is not None:
        _emit_task_card_changed(session_id, card_name_for_event, change_kind, system_sessions_dir)
    return True


def delete_task(
    session_id: str,
    task_name: str,
    sessions_dir: Path,
    system_sessions_dir: Path | None = None,
) -> bool:
    _validate_session_id(session_id)
    from butterfly.session_engine.task_cards import delete_card
    session_dir = sessions_dir / session_id
    if not session_dir.exists():
        return False
    deleted = delete_card(session_dir / 'core' / 'tasks', task_name)
    if deleted:
        _emit_task_card_changed(session_id, task_name, "deleted", system_sessions_dir)
    return deleted
