import pytest

import butterfly.session_engine.agent_state as ms
from butterfly.session_engine.session_init import init_session


def _seed_agent(tmp_path):
    agent_base = tmp_path / 'agenthub'
    ent = agent_base / 'demo'
    (ent / 'prompts').mkdir(parents=True)
    (ent / 'prompts' / 'system.md').write_text('sys\n', encoding='utf-8')
    (ent / 'prompts' / 'task.md').write_text('task\n', encoding='utf-8')
    (ent / 'prompts' / 'env.md').write_text('env\n', encoding='utf-8')
    (ent / 'tools.md').write_text('bash\n', encoding='utf-8')
    (ent / 'skills.md').write_text('', encoding='utf-8')
    (ent / 'config.yaml').write_text('name: demo\nmodel: claude-sonnet-4-6\nprovider: anthropic\n', encoding='utf-8')
    return agent_base


def test_init_session_seeds_memory_from_meta_session(tmp_path):
    agent_base = _seed_agent(tmp_path)
    (tmp_path / 'sessions' / 'demo_meta' / 'core' / 'memory').mkdir(parents=True)
    (tmp_path / 'sessions' / 'demo_meta' / 'playground').mkdir(parents=True)
    (tmp_path / 'sessions' / 'demo_meta' / 'core' / 'memory.md').write_text('meta primary', encoding='utf-8')
    (tmp_path / 'sessions' / 'demo_meta' / 'core' / 'memory' / 'layer.md').write_text('meta layer', encoding='utf-8')
    (tmp_path / 'sessions' / 'demo_meta' / 'playground' / 'seed.txt').write_text('seed', encoding='utf-8')

    ms._SESSIONS_DIR = tmp_path / 'sessions'
    init_session('s1', 'demo', sessions_base=tmp_path / 'sessions', system_sessions_base=tmp_path / '_sessions', agent_base=agent_base)

    core = tmp_path / 'sessions' / 's1' / 'core'
    assert (core / 'memory.md').read_text(encoding='utf-8') == 'meta primary'
    assert (core / 'memory' / 'layer.md').read_text(encoding='utf-8') == 'meta layer'
    assert (tmp_path / 'sessions' / 's1' / 'playground' / 'seed.txt').read_text(encoding='utf-8') == 'seed'


def test_init_session_auto_populates_meta_when_config_empty(tmp_path):
    """init_session populates meta when config.yaml is empty (replaces .agent_synced check)."""
    agent_base = _seed_agent(tmp_path)
    ms._SESSIONS_DIR = tmp_path / 'sessions'
    init_session('s1', 'demo', sessions_base=tmp_path / 'sessions', system_sessions_base=tmp_path / '_sessions', agent_base=agent_base)
    meta = tmp_path / 'sessions' / 'demo_meta' / 'core'
    # config.yaml should exist in meta (copied from agent)
    assert (meta / 'config.yaml').exists()
    assert (tmp_path / 'sessions' / 's1' / 'core' / 'system.md').read_text(encoding='utf-8').strip() == 'sys'
