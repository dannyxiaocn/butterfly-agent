"""Tests for state_diff built-in tool."""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import patch


@pytest.mark.asyncio
async def test_state_diff_no_session(tmp_path):
    """Returns error when NUTSHELL_SESSION_ID is not set."""
    from nutshell.tool_engine.providers.state_diff import state_diff

    with patch.dict("os.environ", {}, clear=True):
        result = await state_diff(key="test", content="hello", _sessions_base=tmp_path)

    assert "Error" in result
    assert "NUTSHELL_SESSION_ID" in result


@pytest.mark.asyncio
async def test_state_diff_first_call_initializes(tmp_path):
    """First call stores state and returns initialized message."""
    from nutshell.tool_engine.providers.state_diff import state_diff

    sessions_base = tmp_path / "sessions"
    sid = "test-session"
    (sessions_base / sid / "core").mkdir(parents=True)

    with patch.dict("os.environ", {"NUTSHELL_SESSION_ID": sid}):
        result = await state_diff(key="ps", content="line1\nline2\n", _sessions_base=sessions_base)

    assert "initialized" in result
    assert "'ps'" in result
    # State file must be written
    state_file = sessions_base / sid / "core" / "state" / "ps.txt"
    assert state_file.exists()
    assert state_file.read_text() == "line1\nline2\n"


@pytest.mark.asyncio
async def test_state_diff_no_change(tmp_path):
    """Returns '(no change)' when content is identical."""
    from nutshell.tool_engine.providers.state_diff import state_diff

    sessions_base = tmp_path / "sessions"
    sid = "test-session"
    (sessions_base / sid / "core" / "state").mkdir(parents=True)
    (sessions_base / sid / "core" / "state" / "disk.txt").write_text("100G free\n")

    with patch.dict("os.environ", {"NUTSHELL_SESSION_ID": sid}):
        result = await state_diff(key="disk", content="100G free\n", _sessions_base=sessions_base)

    assert "no change" in result
    assert "'disk'" in result


@pytest.mark.asyncio
async def test_state_diff_returns_diff(tmp_path):
    """Returns unified diff when content changed."""
    from nutshell.tool_engine.providers.state_diff import state_diff

    sessions_base = tmp_path / "sessions"
    sid = "test-session"
    (sessions_base / sid / "core" / "state").mkdir(parents=True)
    old = "proc A\nproc B\nproc C\n"
    (sessions_base / sid / "core" / "state" / "ps.txt").write_text(old)

    new = "proc A\nproc B\nproc D\n"
    with patch.dict("os.environ", {"NUTSHELL_SESSION_ID": sid}):
        result = await state_diff(key="ps", content=new, _sessions_base=sessions_base)

    # Should contain diff markers
    assert "-proc C" in result
    assert "+proc D" in result
    # State file should be updated
    state_file = sessions_base / sid / "core" / "state" / "ps.txt"
    assert state_file.read_text() == new


@pytest.mark.asyncio
async def test_state_diff_creates_state_dir(tmp_path):
    """Creates core/state/ directory automatically."""
    from nutshell.tool_engine.providers.state_diff import state_diff

    sessions_base = tmp_path / "sessions"
    sid = "test-session"
    (sessions_base / sid / "core").mkdir(parents=True)
    # Note: state/ directory does NOT exist yet

    with patch.dict("os.environ", {"NUTSHELL_SESSION_ID": sid}):
        result = await state_diff(key="git", content="clean\n", _sessions_base=sessions_base)

    state_dir = sessions_base / sid / "core" / "state"
    assert state_dir.is_dir()
    assert (state_dir / "git.txt").exists()
    assert "initialized" in result


@pytest.mark.asyncio
async def test_state_diff_second_call_updates_snapshot(tmp_path):
    """After diff, the snapshot is updated to the new content."""
    from nutshell.tool_engine.providers.state_diff import state_diff

    sessions_base = tmp_path / "sessions"
    sid = "test-session"
    (sessions_base / sid / "core").mkdir(parents=True)

    with patch.dict("os.environ", {"NUTSHELL_SESSION_ID": sid}):
        await state_diff(key="status", content="v1\n", _sessions_base=sessions_base)
        await state_diff(key="status", content="v2\n", _sessions_base=sessions_base)
        # Third call: v2 is now the baseline, v3 vs v2
        result = await state_diff(key="status", content="v3\n", _sessions_base=sessions_base)

    assert "-v2" in result
    assert "+v3" in result


@pytest.mark.asyncio
async def test_state_diff_registered_as_builtin():
    """state_diff is registered in the global tool registry."""
    from nutshell.tool_engine.registry import get_builtin

    impl = get_builtin("state_diff")
    assert impl is not None
    assert callable(impl)


def test_state_diff_json_schema_exists():
    """entity/agent/tools/state_diff.json exists and is valid."""
    import json
    schema_path = Path(__file__).parent.parent / "entity" / "agent" / "tools" / "state_diff.json"
    assert schema_path.exists(), "state_diff.json not found"
    data = json.loads(schema_path.read_text())
    assert data["name"] == "state_diff"
    assert "key" in data["input_schema"]["properties"]
    assert "content" in data["input_schema"]["properties"]
    assert data["input_schema"]["required"] == ["key", "content"]
