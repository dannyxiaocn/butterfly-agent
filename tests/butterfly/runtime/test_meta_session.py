from pathlib import Path

import pytest

from butterfly.session_engine.agent_state import (
    ensure_meta_session,
    get_meta_dir,
    get_meta_session_id,
    populate_meta_from_agent,
    sync_from_agent,
)


def _seed_agent(tmp_path: Path):
    agent_base = tmp_path / 'agenthub'
    agt = agent_base / 'demo'
    (agt / 'prompts').mkdir(parents=True)
    (agt / 'prompts' / 'system.md').write_text('sys v1\n', encoding='utf-8')
    (agt / 'prompts' / 'task.md').write_text('task\n', encoding='utf-8')
    (agt / 'prompts' / 'env.md').write_text('env\n', encoding='utf-8')
    (agt / 'tools.md').write_text('bash\n', encoding='utf-8')
    (agt / 'skills.md').write_text('alpha\n', encoding='utf-8')
    (agt / 'config.yaml').write_text('name: demo\nmodel: m1\nprovider: p1\n', encoding='utf-8')
    return agent_base


def test_ensure_meta_session_creates_structure(tmp_path, monkeypatch):
    monkeypatch.setattr('butterfly.session_engine.agent_state._SESSIONS_DIR', tmp_path / 'sessions')
    meta_dir = ensure_meta_session('agent')
    assert meta_dir == tmp_path / 'sessions' / 'agent_meta'
    assert (meta_dir / 'playground').is_dir()
    assert (meta_dir / 'core' / 'memory.md').exists()


def test_get_meta_session_id_suffix():
    assert get_meta_session_id('agent') == 'agent_meta'


def test_sync_from_agent_bootstraps_memory_when_empty(tmp_path, monkeypatch):
    agent_base = tmp_path / 'agenthub'
    (agent_base / 'demo' / 'memory').mkdir(parents=True)
    (agent_base / 'demo' / 'memory.md').write_text('primary', encoding='utf-8')
    (agent_base / 'demo' / 'memory' / 'notes.md').write_text('layer', encoding='utf-8')
    monkeypatch.setattr('butterfly.session_engine.agent_state._SESSIONS_DIR', tmp_path / 'sessions')
    sync_from_agent('demo', agent_base)
    meta_dir = get_meta_dir('demo')
    assert (meta_dir / 'core' / 'memory.md').read_text(encoding='utf-8') == 'primary'
    assert (meta_dir / 'core' / 'memory' / 'notes.md').read_text(encoding='utf-8') == 'layer'


def test_populate_meta_copies_agent_content(tmp_path, monkeypatch):
    agent_base = _seed_agent(tmp_path)
    monkeypatch.setattr('butterfly.session_engine.agent_state._SESSIONS_DIR', tmp_path / 'sessions')
    monkeypatch.setattr('butterfly.session_engine.agent_state._SYSTEM_SESSIONS_DIR', tmp_path / '_sessions')
    populate_meta_from_agent('demo', agent_base)
    meta_dir = get_meta_dir('demo')
    # config.yaml should be copied
    assert (meta_dir / 'core' / 'config.yaml').exists()
    assert (meta_dir / 'core' / 'system.md').read_text(encoding='utf-8') == 'sys v1\n'
    assert (meta_dir / 'core' / 'tools.md').read_text(encoding='utf-8') == 'bash\n'
    assert (meta_dir / 'core' / 'skills.md').read_text(encoding='utf-8') == 'alpha\n'


def test_sync_from_agent_bootstraps_playground_when_empty(tmp_path, monkeypatch):
    agent_base = tmp_path / 'agenthub'
    playground_dir = agent_base / 'demo' / 'playground' / 'shared'
    playground_dir.mkdir(parents=True)
    (playground_dir / 'seed.txt').write_text('hello', encoding='utf-8')
    monkeypatch.setattr('butterfly.session_engine.agent_state._SESSIONS_DIR', tmp_path / 'sessions')
    sync_from_agent('demo', agent_base)
    meta_dir = get_meta_dir('demo')
    assert (meta_dir / 'playground' / 'shared' / 'seed.txt').read_text(encoding='utf-8') == 'hello'


def test_sync_from_agent_does_not_overwrite_meta_playground(tmp_path, monkeypatch):
    agent_base = tmp_path / 'agenthub'
    playground_dir = agent_base / 'demo' / 'playground'
    playground_dir.mkdir(parents=True)
    (playground_dir / 'config.txt').write_text('agent version', encoding='utf-8')
    monkeypatch.setattr('butterfly.session_engine.agent_state._SESSIONS_DIR', tmp_path / 'sessions')
    meta_dir = ensure_meta_session('demo')
    (meta_dir / 'playground' / 'config.txt').write_text('meta version', encoding='utf-8')
    sync_from_agent('demo', agent_base)
    assert (meta_dir / 'playground' / 'config.txt').read_text(encoding='utf-8') == 'meta version'
