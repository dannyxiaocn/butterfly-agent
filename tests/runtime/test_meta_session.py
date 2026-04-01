from pathlib import Path

import pytest

from nutshell.runtime.meta_session import (
    MetaAlignmentError,
    check_meta_alignment,
    compute_meta_diffs,
    ensure_meta_session,
    get_meta_dir,
    get_meta_session_id,
    populate_meta_from_entity,
    sync_entity_to_meta,
    sync_from_entity,
    sync_meta_to_entity,
)


def _seed_entity(tmp_path: Path):
    entity_base = tmp_path / 'entity'
    ent = entity_base / 'demo'
    (ent / 'prompts').mkdir(parents=True)
    (ent / 'tools').mkdir()
    (ent / 'skills' / 'alpha').mkdir(parents=True)
    (ent / 'prompts' / 'system.md').write_text('sys v1\n', encoding='utf-8')
    (ent / 'prompts' / 'heartbeat.md').write_text('beat\n', encoding='utf-8')
    (ent / 'prompts' / 'session.md').write_text('sess\n', encoding='utf-8')
    (ent / 'tools' / 'bash.json').write_text('{"name":"bash","description":"x","input_schema":{"type":"object"}}\n', encoding='utf-8')
    (ent / 'skills' / 'alpha' / 'SKILL.md').write_text('# alpha\n', encoding='utf-8')
    (ent / 'agent.yaml').write_text('name: demo\nmodel: m1\nprovider: p1\n', encoding='utf-8')
    return entity_base


def test_ensure_meta_session_creates_structure(tmp_path, monkeypatch):
    monkeypatch.setattr('nutshell.runtime.meta_session._SESSIONS_DIR', tmp_path / 'sessions')
    meta_dir = ensure_meta_session('agent')
    assert meta_dir == tmp_path / 'sessions' / 'agent_meta'
    assert (meta_dir / 'playground').is_dir()
    assert (meta_dir / 'core' / 'memory.md').exists()


def test_get_meta_session_id_suffix():
    assert get_meta_session_id('agent') == 'agent_meta'


def test_sync_from_entity_bootstraps_memory_when_empty(tmp_path, monkeypatch):
    entity_base = tmp_path / 'entity'
    (entity_base / 'demo' / 'memory').mkdir(parents=True)
    (entity_base / 'demo' / 'memory.md').write_text('primary', encoding='utf-8')
    (entity_base / 'demo' / 'memory' / 'notes.md').write_text('layer', encoding='utf-8')
    monkeypatch.setattr('nutshell.runtime.meta_session._SESSIONS_DIR', tmp_path / 'sessions')
    sync_from_entity('demo', entity_base)
    meta_dir = get_meta_dir('demo')
    assert (meta_dir / 'core' / 'memory.md').read_text(encoding='utf-8') == 'primary'
    assert (meta_dir / 'core' / 'memory' / 'notes.md').read_text(encoding='utf-8') == 'layer'


def test_populate_and_compute_diffs_and_check(tmp_path, monkeypatch):
    entity_base = _seed_entity(tmp_path)
    monkeypatch.setattr('nutshell.runtime.meta_session._SESSIONS_DIR', tmp_path / 'sessions')
    populate_meta_from_entity('demo', entity_base)
    meta_dir = get_meta_dir('demo')
    assert (meta_dir / 'core' / '.entity_synced').exists()
    assert compute_meta_diffs('demo', entity_base) == []
    check_meta_alignment('demo', entity_base)
    (meta_dir / 'core' / 'system.md').write_text('sys v2\n', encoding='utf-8')
    diffs = compute_meta_diffs('demo', entity_base)
    assert diffs and diffs[0]['path'] == 'core/system.md'
    with pytest.raises(MetaAlignmentError):
        check_meta_alignment('demo', entity_base)


def test_tools_json_normalized_no_false_diff(tmp_path, monkeypatch):
    entity_base = _seed_entity(tmp_path)
    monkeypatch.setattr('nutshell.runtime.meta_session._SESSIONS_DIR', tmp_path / 'sessions')
    populate_meta_from_entity('demo', entity_base)
    meta_tool = get_meta_dir('demo') / 'core' / 'tools' / 'bash.json'
    meta_tool.write_text('{\n  "description": "x", "input_schema": {"type":"object"}, "name": "bash"\n}\n', encoding='utf-8')
    assert compute_meta_diffs('demo', entity_base) == []


def test_sync_entity_to_meta_and_sync_meta_to_entity(tmp_path, monkeypatch):
    entity_base = _seed_entity(tmp_path)
    monkeypatch.setattr('nutshell.runtime.meta_session._SESSIONS_DIR', tmp_path / 'sessions')
    populate_meta_from_entity('demo', entity_base)
    meta_dir = get_meta_dir('demo')
    (meta_dir / 'core' / 'system.md').write_text('meta wins\n', encoding='utf-8')
    sync_meta_to_entity('demo', entity_base)
    assert (entity_base / 'demo' / 'prompts' / 'system.md').read_text(encoding='utf-8') == 'meta wins\n'
    (entity_base / 'demo' / 'prompts' / 'system.md').write_text('entity wins\n', encoding='utf-8')
    sync_entity_to_meta('demo', entity_base)
    assert (meta_dir / 'core' / 'system.md').read_text(encoding='utf-8') == 'entity wins'
