from pathlib import Path

import pytest

from nutshell.session_engine.entity_state import (
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
    monkeypatch.setattr('nutshell.session_engine.entity_state._SESSIONS_DIR', tmp_path / 'sessions')
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
    monkeypatch.setattr('nutshell.session_engine.entity_state._SESSIONS_DIR', tmp_path / 'sessions')
    sync_from_entity('demo', entity_base)
    meta_dir = get_meta_dir('demo')
    assert (meta_dir / 'core' / 'memory.md').read_text(encoding='utf-8') == 'primary'
    assert (meta_dir / 'core' / 'memory' / 'notes.md').read_text(encoding='utf-8') == 'layer'


def test_populate_and_compute_diffs_and_check(tmp_path, monkeypatch):
    entity_base = _seed_entity(tmp_path)
    monkeypatch.setattr('nutshell.session_engine.entity_state._SESSIONS_DIR', tmp_path / 'sessions')
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
    monkeypatch.setattr('nutshell.session_engine.entity_state._SESSIONS_DIR', tmp_path / 'sessions')
    populate_meta_from_entity('demo', entity_base)
    meta_tool = get_meta_dir('demo') / 'core' / 'tools' / 'bash.json'
    meta_tool.write_text('{\n  "description": "x", "input_schema": {"type":"object"}, "name": "bash"\n}\n', encoding='utf-8')
    assert compute_meta_diffs('demo', entity_base) == []


def test_sync_entity_to_meta_and_sync_meta_to_entity(tmp_path, monkeypatch):
    entity_base = _seed_entity(tmp_path)
    monkeypatch.setattr('nutshell.session_engine.entity_state._SESSIONS_DIR', tmp_path / 'sessions')
    populate_meta_from_entity('demo', entity_base)
    meta_dir = get_meta_dir('demo')
    (meta_dir / 'core' / 'system.md').write_text('meta wins\n', encoding='utf-8')
    sync_meta_to_entity('demo', entity_base)
    assert (entity_base / 'demo' / 'prompts' / 'system.md').read_text(encoding='utf-8') == 'meta wins\n'
    (entity_base / 'demo' / 'prompts' / 'system.md').write_text('entity wins\n', encoding='utf-8')
    sync_entity_to_meta('demo', entity_base)
    assert (meta_dir / 'core' / 'system.md').read_text(encoding='utf-8') == 'entity wins'


def test_sync_from_entity_bootstraps_playground_when_empty(tmp_path, monkeypatch):
    entity_base = tmp_path / 'entity'
    playground_dir = entity_base / 'demo' / 'playground' / 'shared'
    playground_dir.mkdir(parents=True)
    (playground_dir / 'seed.txt').write_text('hello', encoding='utf-8')
    monkeypatch.setattr('nutshell.session_engine.entity_state._SESSIONS_DIR', tmp_path / 'sessions')
    sync_from_entity('demo', entity_base)
    meta_dir = get_meta_dir('demo')
    assert (meta_dir / 'playground' / 'shared' / 'seed.txt').read_text(encoding='utf-8') == 'hello'


def test_sync_from_entity_does_not_overwrite_meta_playground(tmp_path, monkeypatch):
    entity_base = tmp_path / 'entity'
    playground_dir = entity_base / 'demo' / 'playground'
    playground_dir.mkdir(parents=True)
    (playground_dir / 'config.txt').write_text('entity version', encoding='utf-8')
    monkeypatch.setattr('nutshell.session_engine.entity_state._SESSIONS_DIR', tmp_path / 'sessions')
    meta_dir = ensure_meta_session('demo')
    (meta_dir / 'playground' / 'config.txt').write_text('meta version', encoding='utf-8')
    sync_from_entity('demo', entity_base)
    assert (meta_dir / 'playground' / 'config.txt').read_text(encoding='utf-8') == 'meta version'


def test_sync_from_entity_preserves_own_memory_when_parent_changes(tmp_path, monkeypatch):
    entity_base = tmp_path / 'entity'
    parent = entity_base / 'parent'
    (parent / 'memory').mkdir(parents=True)
    (parent / 'memory.md').write_text('parent-v1', encoding='utf-8')
    (parent / 'agent.yaml').write_text('name: parent\n', encoding='utf-8')
    child = entity_base / 'child'
    child.mkdir(parents=True)
    (child / 'agent.yaml').write_text(
        """name: child
extends: parent
own:
  - memory
""",
        encoding='utf-8',
    )
    monkeypatch.setattr('nutshell.session_engine.entity_state._SESSIONS_DIR', tmp_path / 'sessions')
    sync_from_entity('child', entity_base)
    meta_dir = get_meta_dir('child')
    (meta_dir / 'core' / 'memory.md').write_text('child-own', encoding='utf-8')
    (parent / 'memory.md').write_text('parent-v2', encoding='utf-8')
    sync_from_entity('child', entity_base)
    assert (meta_dir / 'core' / 'memory.md').read_text(encoding='utf-8') == 'child-own'


def test_sync_from_entity_updates_inherited_playground_without_touching_own(tmp_path, monkeypatch):
    entity_base = tmp_path / 'entity'
    parent = entity_base / 'parent'
    (parent / 'playground').mkdir(parents=True)
    (parent / 'playground' / 'shared.txt').write_text('parent-shared', encoding='utf-8')
    (parent / 'agent.yaml').write_text('name: parent\n', encoding='utf-8')
    child = entity_base / 'child'
    (child / 'playground').mkdir(parents=True)
    (child / 'playground' / 'own.txt').write_text('child-own', encoding='utf-8')
    (child / 'agent.yaml').write_text(
        """name: child
extends: parent
link:
  - playground
own:
  - memory
""",
        encoding='utf-8',
    )
    monkeypatch.setattr('nutshell.session_engine.entity_state._SESSIONS_DIR', tmp_path / 'sessions')
    sync_from_entity('child', entity_base)
    meta_dir = get_meta_dir('child')
    assert (meta_dir / 'playground' / 'own.txt').read_text(encoding='utf-8') == 'child-own'
    assert (meta_dir / 'playground' / 'shared.txt').read_text(encoding='utf-8') == 'parent-shared'
