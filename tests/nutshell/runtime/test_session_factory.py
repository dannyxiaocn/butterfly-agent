import pytest

import nutshell.session_engine.entity_state as ms
from nutshell.session_engine.session_init import init_session


def _seed_entity(tmp_path):
    entity_base = tmp_path / 'entity'
    ent = entity_base / 'demo'
    (ent / 'prompts').mkdir(parents=True)
    (ent / 'tools').mkdir()
    (ent / 'skills' / 'alpha').mkdir(parents=True)
    (ent / 'prompts' / 'system.md').write_text('sys\n', encoding='utf-8')
    (ent / 'prompts' / 'heartbeat.md').write_text('beat\n', encoding='utf-8')
    (ent / 'prompts' / 'session.md').write_text('sess\n', encoding='utf-8')
    (ent / 'tools' / 'bash.json').write_text('{"name":"bash","description":"x","input_schema":{"type":"object"}}', encoding='utf-8')
    (ent / 'skills' / 'alpha' / 'SKILL.md').write_text('# alpha\n', encoding='utf-8')
    (ent / 'agent.yaml').write_text('name: demo\nmodel: claude-sonnet-4-6\nprovider: anthropic\ntools: []\nskills: []\n', encoding='utf-8')
    return entity_base


def test_init_session_seeds_memory_from_meta_session(tmp_path):
    entity_base = _seed_entity(tmp_path)
    (tmp_path / 'sessions' / 'demo_meta' / 'core' / 'memory').mkdir(parents=True)
    (tmp_path / 'sessions' / 'demo_meta' / 'playground').mkdir(parents=True)
    (tmp_path / 'sessions' / 'demo_meta' / 'core' / 'memory.md').write_text('meta primary', encoding='utf-8')
    (tmp_path / 'sessions' / 'demo_meta' / 'core' / 'memory' / 'layer.md').write_text('meta layer', encoding='utf-8')
    (tmp_path / 'sessions' / 'demo_meta' / 'playground' / 'seed.txt').write_text('seed', encoding='utf-8')

    ms._SESSIONS_DIR = tmp_path / 'sessions'
    init_session('s1', 'demo', sessions_base=tmp_path / 'sessions', system_sessions_base=tmp_path / '_sessions', entity_base=entity_base)

    core = tmp_path / 'sessions' / 's1' / 'core'
    assert (core / 'memory.md').read_text(encoding='utf-8') == 'meta primary'
    assert (core / 'memory' / 'layer.md').read_text(encoding='utf-8') == 'meta layer'
    assert (tmp_path / 'sessions' / 's1' / 'playground' / 'seed.txt').read_text(encoding='utf-8') == 'seed'


def test_init_session_auto_populates_meta_when_unsynced(tmp_path):
    entity_base = _seed_entity(tmp_path)
    ms._SESSIONS_DIR = tmp_path / 'sessions'
    init_session('s1', 'demo', sessions_base=tmp_path / 'sessions', system_sessions_base=tmp_path / '_sessions', entity_base=entity_base)
    meta = tmp_path / 'sessions' / 'demo_meta' / 'core'
    assert (meta / '.entity_synced').exists()
    assert (tmp_path / 'sessions' / 's1' / 'core' / 'system.md').read_text(encoding='utf-8').strip() == 'sys'
