"""Tests for gene commands in meta_session."""
from pathlib import Path
from unittest.mock import patch

import pytest

from nutshell.session_engine.entity_state import (
    _create_meta_venv,
    _load_gene_commands,
    ensure_gene_initialized,
    ensure_meta_session,
    run_gene_commands,
)


def _seed_entity_with_gene(tmp_path: Path, gene: list[str] | None = None):
    """Create a minimal entity with optional gene commands."""
    entity_base = tmp_path / 'entity'
    ent = entity_base / 'demo'
    ent.mkdir(parents=True)
    import yaml
    manifest = {'name': 'demo', 'model': 'm1', 'provider': 'p1'}
    if gene is not None:
        manifest['gene'] = gene
    (ent / 'agent.yaml').write_text(yaml.dump(manifest), encoding='utf-8')
    return entity_base


def test_gene_commands_run_in_playground(tmp_path, monkeypatch):
    """Verify gene commands execute with cwd=playground_dir and venv env."""
    entity_base = _seed_entity_with_gene(tmp_path, gene=['echo hello', 'echo world'])
    monkeypatch.setattr('nutshell.session_engine.entity_state._SESSIONS_DIR', tmp_path / 'sessions')

    calls = []
    original_run = __import__('subprocess').run

    def mock_run(*args, **kwargs):
        # Let venv creation pass through
        if isinstance(args[0], list) and '-m' in args[0] and 'venv' in args[0]:
            return original_run(*args, **kwargs)
        calls.append((args, kwargs))
        from subprocess import CompletedProcess
        return CompletedProcess(args=args[0], returncode=0, stdout='', stderr='')

    monkeypatch.setattr('subprocess.run', mock_run)

    run_gene_commands('demo', entity_base=entity_base, s_base=tmp_path / 'sessions')

    meta_dir = tmp_path / 'sessions' / 'demo_meta'
    playground_dir = meta_dir / 'playground'

    assert len(calls) == 2
    # Check cwd is the playground
    assert calls[0][1]['cwd'] == str(playground_dir)
    assert calls[1][1]['cwd'] == str(playground_dir)
    # Check shell=True
    assert calls[0][1]['shell'] is True
    # Check VIRTUAL_ENV is set in env
    assert 'VIRTUAL_ENV' in calls[0][1]['env']
    # Check marker was written
    marker = meta_dir / 'core' / '.gene_initialized'
    assert marker.exists()


def test_ensure_gene_initialized_skips_if_marker_exists(tmp_path, monkeypatch):
    """Verify that if .gene_initialized exists, subprocess.run is not called for gene."""
    entity_base = _seed_entity_with_gene(tmp_path, gene=['echo should not run'])
    monkeypatch.setattr('nutshell.session_engine.entity_state._SESSIONS_DIR', tmp_path / 'sessions')

    # Pre-create meta session with marker
    meta_dir = ensure_meta_session('demo', s_base=tmp_path / 'sessions')
    marker = meta_dir / 'core' / '.gene_initialized'
    marker.write_text('demo', encoding='utf-8')

    calls = []
    original_run = __import__('subprocess').run

    def mock_run(*args, **kwargs):
        if isinstance(args[0], list) and '-m' in args[0] and 'venv' in args[0]:
            return original_run(*args, **kwargs)
        calls.append((args, kwargs))
        from subprocess import CompletedProcess
        return CompletedProcess(args=args[0], returncode=0, stdout='', stderr='')

    monkeypatch.setattr('subprocess.run', mock_run)

    ensure_gene_initialized('demo', entity_base=entity_base, s_base=tmp_path / 'sessions')

    # No gene commands should have been called
    assert len(calls) == 0


def test_load_gene_commands_empty_when_no_gene(tmp_path, monkeypatch):
    """Verify _load_gene_commands returns [] when no gene field."""
    entity_base = _seed_entity_with_gene(tmp_path, gene=None)
    monkeypatch.setattr('nutshell.session_engine.entity_state._SESSIONS_DIR', tmp_path / 'sessions')
    result = _load_gene_commands('demo', entity_base=entity_base)
    assert result == []


def test_run_gene_commands_no_gene_field(tmp_path, monkeypatch):
    """Verify run_gene_commands is a no-op when no gene field exists."""
    entity_base = _seed_entity_with_gene(tmp_path, gene=None)
    monkeypatch.setattr('nutshell.session_engine.entity_state._SESSIONS_DIR', tmp_path / 'sessions')

    # Should not raise, should not write marker
    run_gene_commands('demo', entity_base=entity_base, s_base=tmp_path / 'sessions')
    meta_dir = tmp_path / 'sessions' / 'demo_meta'
    marker = meta_dir / 'core' / '.gene_initialized'
    assert not marker.exists()


def test_gene_command_failure_does_not_raise(tmp_path, monkeypatch):
    """Verify a failing gene command prints error but does not raise."""
    entity_base = _seed_entity_with_gene(tmp_path, gene=['false'])
    monkeypatch.setattr('nutshell.session_engine.entity_state._SESSIONS_DIR', tmp_path / 'sessions')

    # Should not raise even though 'false' exits with code 1
    run_gene_commands('demo', entity_base=entity_base, s_base=tmp_path / 'sessions')

    # Marker should still be written
    meta_dir = tmp_path / 'sessions' / 'demo_meta'
    marker = meta_dir / 'core' / '.gene_initialized'
    assert marker.exists()
