from __future__ import annotations

import json

import pytest

from nutshell.tool_engine.providers.archive_session import archive_session
from nutshell.tool_engine.providers.get_session_info import get_session_info
from nutshell.tool_engine.providers.list_child_sessions import list_child_sessions
from nutshell.tool_engine.registry import get_builtin


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


def _make_session(tmp_path, sid: str, *, entity='agent', status='active', last_run='2026-01-01T00:00:00', tasks='', turns=None, memory_files=None):
    sessions_base = tmp_path / 'sessions'
    system_base = tmp_path / '_sessions'
    _write_json(system_base / sid / 'manifest.json', {
        'session_id': sid,
        'entity': entity,
        'created_at': '2026-01-01T00:00:00',
    })
    _write_json(system_base / sid / 'status.json', {
        'status': status,
        'last_run_at': last_run,
    })
    (sessions_base / sid / 'core').mkdir(parents=True, exist_ok=True)
    (sessions_base / sid / 'core' / 'tasks.md').write_text(tasks, encoding='utf-8')
    if turns is not None:
        (system_base / sid / 'context.jsonl').write_text(
            '\n'.join(json.dumps(t, ensure_ascii=False) for t in turns) + '\n',
            encoding='utf-8',
        )
    if memory_files:
        mem = sessions_base / sid / 'core' / 'memory'
        mem.mkdir(parents=True, exist_ok=True)
        for name in memory_files:
            (mem / name).write_text('# mem', encoding='utf-8')
    return sessions_base, system_base


@pytest.mark.asyncio
async def test_list_child_sessions_filters_entity_meta_and_archived(tmp_path, monkeypatch):
    sessions_base, system_base = _make_session(tmp_path, 'child-1', entity='demo', tasks='- [ ] one\n\nline\n')
    _make_session(tmp_path, 'child-2_meta', entity='demo')
    _make_session(tmp_path, 'other-1', entity='other')
    archived = tmp_path / '_archived' / '_sessions' / 'child-archived'
    _write_json(archived / 'manifest.json', {'session_id': 'child-archived', 'entity': 'demo'})
    _make_session(tmp_path, 'child-archived', entity='demo')

    monkeypatch.setenv('NUTSHELL_ENTITY', 'demo')
    result = await list_child_sessions(_sessions_base=system_base, _system_base=system_base)
    data = json.loads(result)
    assert data == [{
        'session_id': 'child-1',
        'status': 'active',
        'last_run': '2026-01-01T00:00:00',
        'task_count': 2,
    }]


@pytest.mark.asyncio
async def test_get_session_info_returns_recent_turns_tasks_and_memory(tmp_path):
    long_task = 'A' * 700
    turns = [
        {'type': 'user_input', 'content': 'ignore me'},
        {'type': 'turn', 'triggered_by': 'heartbeat', 'messages': [{'role': 'assistant', 'content': 'reply-1'}]},
        {'type': 'turn', 'triggered_by': 'user_input', 'messages': [{'role': 'assistant', 'content': [{'type': 'text', 'text': 'reply-2'}]}]},
        {'type': 'turn', 'triggered_by': 'agent', 'messages': [{'role': 'assistant', 'content': 'x' * 120}]},
        {'type': 'turn', 'triggered_by': 'timer', 'messages': [{'role': 'assistant', 'content': 'reply-4'}]},
    ]
    sessions_base, system_base = _make_session(
        tmp_path,
        's1',
        entity='demo',
        tasks=long_task,
        turns=turns,
        memory_files=['a.md', 'b.md'],
    )

    result = await get_session_info('s1', _sessions_base=sessions_base, _system_base=system_base)
    data = json.loads(result)
    assert data['session_id'] == 's1'
    assert data['entity'] == 'demo'
    assert data['status'] == 'active'
    assert len(data['recent_turns']) == 3
    assert [t['triggered_by'] for t in data['recent_turns']] == ['user_input', 'agent', 'timer']
    assert data['recent_turns'][0]['content_preview'] == 'reply-2'
    assert len(data['recent_turns'][1]['content_preview']) == 100
    assert len(data['tasks']) == 500
    assert data['memory_files'] == ['a.md', 'b.md']


@pytest.mark.asyncio
async def test_archive_session_moves_dirs_and_writes_info(tmp_path):
    sessions_base, system_base = _make_session(tmp_path, 's2', entity='demo')
    archived_base = tmp_path / '_archived'

    result = await archive_session('s2', reason='done', _sessions_base=sessions_base, _system_base=system_base, _archived_base=archived_base)
    assert result == 'archived s2'
    assert not (sessions_base / 's2').exists()
    assert not (system_base / 's2').exists()
    assert (archived_base / 'sessions' / 's2').exists()
    info = json.loads((archived_base / '_sessions' / 's2' / 'archive_info.json').read_text(encoding='utf-8'))
    assert info['reason'] == 'done'
    assert info['entity'] == 'demo'
    assert info['archived_at']


@pytest.mark.asyncio
async def test_archive_session_missing_returns_error(tmp_path):
    result = await archive_session('missing', _sessions_base=tmp_path / 'sessions', _system_base=tmp_path / '_sessions', _archived_base=tmp_path / '_archived')
    assert result == "Error: session 'missing' not found."


def test_registry_exposes_meta_session_tools():
    assert get_builtin('list_child_sessions') is not None
    assert get_builtin('get_session_info') is not None
    assert get_builtin('archive_session') is not None
