"""Tests for session-level isolated Python venv.

Covers:
1. init_session creates .venv directory
2. BashExecutor injects correct PATH / VIRTUAL_ENV when .venv exists
3. Session bash `which python3` points inside .venv
4. pip install installs into venv, not global
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from nutshell.session_engine.session_init import _create_session_venv, init_session
from nutshell.tool_engine.executor.terminal.bash_terminal import _venv_env, create_bash_tool


# ── 1. init_session creates .venv ──────────────────────────────────────────────

def test_init_session_creates_venv(tmp_path):
    """init_session should create a .venv directory in the session dir."""
    sessions_base = tmp_path / "sessions"
    sys_base = tmp_path / "_sessions"
    entity_base = tmp_path / "entity"

    # Minimal entity so init_session doesn't need a real one
    entity_dir = entity_base / "test_ent"
    entity_dir.mkdir(parents=True)

    sid = init_session(
        "test-001",
        "test_ent",
        sessions_base=sessions_base,
        system_sessions_base=sys_base,
        entity_base=entity_base,
    )
    venv = sessions_base / sid / ".venv"
    assert venv.is_dir(), ".venv directory should exist after init_session"
    assert (venv / "bin" / "python3").exists() or (venv / "bin" / "python").exists(), \
        "venv should contain a python binary"


def test_create_session_venv_idempotent(tmp_path):
    """Calling _create_session_venv twice should not fail."""
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    p1 = _create_session_venv(session_dir)
    p2 = _create_session_venv(session_dir)
    assert p1 == p2
    assert p1.is_dir()


# ── 2. _venv_env injects correct env vars ─────────────────────────────────────

def test_venv_env_returns_none_without_session_id():
    """No NUTSHELL_SESSION_ID → _venv_env returns None."""
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NUTSHELL_SESSION_ID", None)
        assert _venv_env() is None


def test_venv_env_returns_none_without_venv(tmp_path):
    """Session exists but no .venv → _venv_env returns None."""
    sessions_base = tmp_path / "sessions"
    sid = "no-venv-session"
    (sessions_base / sid).mkdir(parents=True)
    with mock.patch("nutshell.tool_engine.executor.terminal.bash_terminal._REPO_ROOT", tmp_path):
        with mock.patch.dict(os.environ, {"NUTSHELL_SESSION_ID": sid}):
            assert _venv_env() is None


def test_venv_env_injects_vars(tmp_path):
    """When .venv exists, _venv_env returns env with VIRTUAL_ENV and PATH prepended."""
    sessions_base = tmp_path / "sessions"
    sid = "venv-session"
    session_dir = sessions_base / sid
    session_dir.mkdir(parents=True)
    venv_path = session_dir / ".venv"
    venv_bin = venv_path / "bin"
    venv_bin.mkdir(parents=True)

    with mock.patch("nutshell.tool_engine.executor.terminal.bash_terminal._REPO_ROOT", tmp_path):
        with mock.patch.dict(os.environ, {"NUTSHELL_SESSION_ID": sid}):
            env = _venv_env()
            assert env is not None
            assert env["VIRTUAL_ENV"] == str(venv_path)
            assert env["PATH"].startswith(str(venv_bin) + ":")
            assert "PYTHONHOME" not in env


def test_venv_env_strips_pythonhome(tmp_path):
    """PYTHONHOME should be removed from the venv env."""
    sessions_base = tmp_path / "sessions"
    sid = "ph-session"
    session_dir = sessions_base / sid
    venv_bin = session_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)

    with mock.patch("nutshell.tool_engine.executor.terminal.bash_terminal._REPO_ROOT", tmp_path):
        with mock.patch.dict(os.environ, {"NUTSHELL_SESSION_ID": sid, "PYTHONHOME": "/bad"}):
            env = _venv_env()
            assert "PYTHONHOME" not in env


# ── 3. Session bash `which python3` points inside .venv ───────────────────────

@pytest.mark.asyncio
async def test_bash_which_python3_in_venv(tmp_path):
    """When venv exists, `which python3` inside bash should resolve to .venv/bin/python3."""
    sessions_base = tmp_path / "sessions"
    sid = "which-test"
    session_dir = sessions_base / sid
    session_dir.mkdir(parents=True)

    # Create a real venv
    _create_session_venv(session_dir)
    venv_path = session_dir / ".venv"

    with mock.patch("nutshell.tool_engine.executor.terminal.bash_terminal._REPO_ROOT", tmp_path):
        with mock.patch.dict(os.environ, {"NUTSHELL_SESSION_ID": sid}):
            tool = create_bash_tool()
            result = await tool.execute(command="which python3")
            assert ".venv" in result, f"Expected .venv in path, got: {result}"
            assert str(venv_path) in result


@pytest.mark.asyncio
async def test_bash_python3_import_sys(tmp_path):
    """python3 inside venv should report sys.prefix as the venv path."""
    sessions_base = tmp_path / "sessions"
    sid = "prefix-test"
    session_dir = sessions_base / sid
    session_dir.mkdir(parents=True)
    _create_session_venv(session_dir)
    venv_path = session_dir / ".venv"

    with mock.patch("nutshell.tool_engine.executor.terminal.bash_terminal._REPO_ROOT", tmp_path):
        with mock.patch.dict(os.environ, {"NUTSHELL_SESSION_ID": sid}):
            tool = create_bash_tool()
            result = await tool.execute(
                command="python3 -c \"import sys; print(sys.prefix)\""
            )
            assert str(venv_path) in result, f"Expected venv prefix, got: {result}"


# ── 4. pip install installs to venv, not global ───────────────────────────────

@pytest.mark.asyncio
async def test_pip_install_to_venv(tmp_path):
    """pip install --target should go into the venv site-packages."""
    sessions_base = tmp_path / "sessions"
    sid = "pip-test"
    session_dir = sessions_base / sid
    session_dir.mkdir(parents=True)
    _create_session_venv(session_dir)
    venv_path = session_dir / ".venv"

    with mock.patch("nutshell.tool_engine.executor.terminal.bash_terminal._REPO_ROOT", tmp_path):
        with mock.patch.dict(os.environ, {"NUTSHELL_SESSION_ID": sid}):
            tool = create_bash_tool()
            # Use pip to show where it would install
            result = await tool.execute(
                command="python3 -c \"import sysconfig; print(sysconfig.get_path('purelib'))\""
            )
            assert str(venv_path) in result, \
                f"pip install target should be inside venv, got: {result}"


# ── 5. No venv → normal behavior (no env injection) ───────────────────────────

@pytest.mark.asyncio
async def test_bash_without_venv_works():
    """Without NUTSHELL_SESSION_ID, bash should still work normally."""
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NUTSHELL_SESSION_ID", None)
        tool = create_bash_tool()
        result = await tool.execute(command="echo works")
        assert "works" in result
        assert "[exit 0]" in result


# ── 6. PTY mode also uses venv ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pty_mode_uses_venv(tmp_path):
    """PTY mode should also inject venv env."""
    sessions_base = tmp_path / "sessions"
    sid = "pty-venv"
    session_dir = sessions_base / sid
    session_dir.mkdir(parents=True)
    _create_session_venv(session_dir)
    venv_path = session_dir / ".venv"

    with mock.patch("nutshell.tool_engine.executor.terminal.bash_terminal._REPO_ROOT", tmp_path):
        with mock.patch.dict(os.environ, {"NUTSHELL_SESSION_ID": sid}):
            tool = create_bash_tool()
            result = await tool.execute(command="which python3", pty=True)
            if "[pty unavailable" in result:
                pytest.skip("PTY not available")
            assert ".venv" in result, f"PTY should use venv, got: {result}"
