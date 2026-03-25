"""Tests for git_checkpoint built-in tool."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nutshell.tool_engine.providers.git_checkpoint import git_checkpoint


# ── helpers ───────────────────────────────────────────────────────────────────

def _init_git_repo(path: Path) -> None:
    """Initialize a minimal git repo with an initial commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(path), check=True, capture_output=True)
    # Initial commit so HEAD exists
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "-A"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"],
                   cwd=str(path), check=True, capture_output=True)


def _make_session(tmp_path: Path, session_id: str = "test-sess") -> Path:
    sessions_base = tmp_path / "sessions"
    session_dir = sessions_base / session_id
    session_dir.mkdir(parents=True)
    return sessions_base


# ── tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_session_env(tmp_path, monkeypatch):
    monkeypatch.delenv("NUTSHELL_SESSION_ID", raising=False)
    result = await git_checkpoint(message="test", _sessions_base=tmp_path)
    assert "Error" in result
    assert "NUTSHELL_SESSION_ID" in result


@pytest.mark.asyncio
async def test_workdir_not_found(tmp_path, monkeypatch):
    sessions_base = _make_session(tmp_path)
    monkeypatch.setenv("NUTSHELL_SESSION_ID", "test-sess")
    result = await git_checkpoint(
        message="test", workdir="nonexistent", _sessions_base=sessions_base
    )
    assert "Error" in result
    assert "not found" in result


@pytest.mark.asyncio
async def test_not_a_git_repo(tmp_path, monkeypatch):
    sessions_base = _make_session(tmp_path)
    monkeypatch.setenv("NUTSHELL_SESSION_ID", "test-sess")
    result = await git_checkpoint(message="test", _sessions_base=sessions_base)
    assert "Error" in result
    assert "git repository" in result


@pytest.mark.asyncio
async def test_nothing_to_commit(tmp_path, monkeypatch):
    sessions_base = _make_session(tmp_path)
    repo = sessions_base / "test-sess" / "playground" / "repo"
    _init_git_repo(repo)
    monkeypatch.setenv("NUTSHELL_SESSION_ID", "test-sess")

    result = await git_checkpoint(
        message="checkpoint", workdir="playground/repo", _sessions_base=sessions_base
    )
    assert "nothing to commit" in result


@pytest.mark.asyncio
async def test_commit_new_file(tmp_path, monkeypatch):
    sessions_base = _make_session(tmp_path)
    repo = sessions_base / "test-sess" / "playground" / "repo"
    _init_git_repo(repo)
    (repo / "new_file.txt").write_text("hello\n")
    monkeypatch.setenv("NUTSHELL_SESSION_ID", "test-sess")

    result = await git_checkpoint(
        message="feat: add new_file", workdir="playground/repo", _sessions_base=sessions_base
    )
    assert "Committed" in result
    assert "feat: add new_file" in result
    # Verify the commit actually exists
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=str(repo), capture_output=True, text=True
    )
    assert "feat: add new_file" in log.stdout


@pytest.mark.asyncio
async def test_commit_modified_file(tmp_path, monkeypatch):
    sessions_base = _make_session(tmp_path)
    repo = sessions_base / "test-sess" / "playground" / "repo"
    _init_git_repo(repo)
    (repo / "README.md").write_text("updated content\n")
    monkeypatch.setenv("NUTSHELL_SESSION_ID", "test-sess")

    result = await git_checkpoint(
        message="docs: update README", workdir="playground/repo", _sessions_base=sessions_base
    )
    assert "Committed" in result
    assert "docs: update README" in result


@pytest.mark.asyncio
async def test_commit_returns_short_hash(tmp_path, monkeypatch):
    sessions_base = _make_session(tmp_path)
    repo = sessions_base / "test-sess" / "playground" / "repo"
    _init_git_repo(repo)
    (repo / "file.txt").write_text("content\n")
    monkeypatch.setenv("NUTSHELL_SESSION_ID", "test-sess")

    result = await git_checkpoint(
        message="add file", workdir="playground/repo", _sessions_base=sessions_base
    )
    # Should contain "Committed <hash>: <message>"
    import re
    assert re.search(r"Committed [0-9a-f]{6,}:", result)


@pytest.mark.asyncio
async def test_default_workdir_is_session_dir(tmp_path, monkeypatch):
    sessions_base = _make_session(tmp_path)
    session_dir = sessions_base / "test-sess"
    _init_git_repo(session_dir)
    (session_dir / "work.txt").write_text("done\n")
    monkeypatch.setenv("NUTSHELL_SESSION_ID", "test-sess")

    result = await git_checkpoint(message="checkpoint", _sessions_base=sessions_base)
    assert "Committed" in result


def test_git_checkpoint_registered_as_builtin():
    from nutshell.tool_engine.registry import get_builtin
    impl = get_builtin("git_checkpoint")
    assert impl is not None
    assert callable(impl)
