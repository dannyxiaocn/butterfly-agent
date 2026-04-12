import json

import pytest

from nutshell.session_engine.entity_state import populate_meta_from_entity
from nutshell.session_engine.session_status import read_session_status, write_session_status


@pytest.mark.asyncio
async def test_watcher_blocks_misaligned_meta_session(tmp_path, monkeypatch, capsys):
    from nutshell.runtime.watcher import SessionWatcher

    entity_base = tmp_path / 'entity'
    ent = entity_base / 'demo'
    (ent / 'prompts').mkdir(parents=True)
    (ent / 'prompts' / 'system.md').write_text('sys\n', encoding='utf-8')
    (ent / 'prompts' / 'heartbeat.md').write_text('beat\n', encoding='utf-8')
    (ent / 'prompts' / 'session.md').write_text('sess\n', encoding='utf-8')
    (ent / 'agent.yaml').write_text('name: demo\nmodel: claude-sonnet-4-6\nprovider: anthropic\n', encoding='utf-8')

    sessions_dir = tmp_path / 'sessions'
    system_dir = tmp_path / '_sessions'
    sessions_dir.mkdir()
    system_dir.mkdir()
    monkeypatch.setattr('nutshell.session_engine.entity_state._SESSIONS_DIR', sessions_dir)
    populate_meta_from_entity('demo', entity_base, sessions_dir)
    (sessions_dir / 'demo_meta' / 'core' / 'system.md').write_text('drift\n', encoding='utf-8')

    sid = 's1'
    sys_session_dir = system_dir / sid
    sys_session_dir.mkdir(parents=True)
    (sys_session_dir / 'manifest.json').write_text(json.dumps({'entity': 'demo'}), encoding='utf-8')
    ses = sessions_dir / sid
    (ses / 'core').mkdir(parents=True)
    (ses / 'core' / 'params.json').write_text(json.dumps({'heartbeat_interval': 10}), encoding='utf-8')
    write_session_status(sys_session_dir, status='active')

    monkeypatch.setattr('nutshell.session_engine.entity_state._REPO_ROOT', tmp_path)
    watcher = SessionWatcher(sessions_dir, system_dir)
    await watcher._start_session(sid, sys_session_dir, {'entity': 'demo'})
    out = capsys.readouterr().out
    assert 'ALIGNMENT CONFLICT' in out
    assert read_session_status(sys_session_dir)['status'] == 'alignment_blocked'
