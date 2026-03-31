
import json

import pytest

from nutshell.tool_engine.providers.update_meta_memory import update_meta_memory
from nutshell.tool_engine.registry import get_builtin


@pytest.mark.asyncio
async def test_update_meta_memory_updates_primary(tmp_path, monkeypatch):
    session_id = 's1'
    manifest_dir = tmp_path / '_sessions' / session_id
    manifest_dir.mkdir(parents=True)
    (manifest_dir / 'manifest.json').write_text(json.dumps({'entity': 'agent'}), encoding='utf-8')
    monkeypatch.setenv('NUTSHELL_SESSION_ID', session_id)
    monkeypatch.setattr('nutshell.tool_engine.providers.update_meta_memory._REPO_ROOT', tmp_path)

    result = await update_meta_memory(content='hello', reason='test', _sessions_base=tmp_path / 'sessions')
    assert 'Meta memory updated' in result
    assert (tmp_path / 'sessions' / 'agent_meta' / 'core' / 'memory.md').read_text(encoding='utf-8') == 'hello'


@pytest.mark.asyncio
async def test_update_meta_memory_updates_layer(tmp_path, monkeypatch):
    session_id = 's2'
    manifest_dir = tmp_path / '_sessions' / session_id
    manifest_dir.mkdir(parents=True)
    (manifest_dir / 'manifest.json').write_text(json.dumps({'entity': 'agent'}), encoding='utf-8')
    monkeypatch.setenv('NUTSHELL_SESSION_ID', session_id)
    monkeypatch.setattr('nutshell.tool_engine.providers.update_meta_memory._REPO_ROOT', tmp_path)

    await update_meta_memory(content='layered', layer='project', reason='test', _sessions_base=tmp_path / 'sessions')
    assert (tmp_path / 'sessions' / 'agent_meta' / 'core' / 'memory' / 'project.md').read_text(encoding='utf-8') == 'layered'


@pytest.mark.asyncio
async def test_update_meta_memory_missing_session_id(monkeypatch):
    monkeypatch.delenv('NUTSHELL_SESSION_ID', raising=False)
    result = await update_meta_memory(content='x', reason='test')
    assert 'NUTSHELL_SESSION_ID is not set' in result


def test_update_meta_memory_registered_as_builtin():
    impl = get_builtin('update_meta_memory')
    assert impl is not None
    assert impl.__name__ == 'update_meta_memory'
