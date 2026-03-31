
from nutshell.runtime.meta_session import ensure_meta_session, get_meta_dir, get_meta_session_id, sync_from_entity


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


def test_sync_from_entity_does_not_overwrite_nonempty_meta(tmp_path, monkeypatch):
    entity_base = tmp_path / 'entity'
    (entity_base / 'demo').mkdir(parents=True)
    (entity_base / 'demo' / 'memory.md').write_text('entity', encoding='utf-8')
    monkeypatch.setattr('nutshell.runtime.meta_session._SESSIONS_DIR', tmp_path / 'sessions')
    meta_dir = ensure_meta_session('demo')
    (meta_dir / 'core' / 'memory.md').write_text('existing', encoding='utf-8')
    sync_from_entity('demo', entity_base)
    assert (meta_dir / 'core' / 'memory.md').read_text(encoding='utf-8') == 'existing'
