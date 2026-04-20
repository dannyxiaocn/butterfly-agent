"""Tests for butterfly.session_engine.task_runner (trigger / end script execution)."""
from __future__ import annotations

import pytest

from butterfly.session_engine.task_runner import run_script


def _write(path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_run_script_start(tmp_path):
    script = tmp_path / "t.trigger.sh"
    _write(script, "echo [start]")
    result = await run_script(script, cwd=tmp_path)
    assert result.tag == "[start]"
    assert result.exit_code == 0
    assert result.timed_out is False


@pytest.mark.asyncio
async def test_run_script_skip(tmp_path):
    script = tmp_path / "t.trigger.sh"
    _write(script, "echo debug; echo [skip]")
    result = await run_script(script, cwd=tmp_path)
    assert result.tag == "[skip]"


@pytest.mark.asyncio
async def test_run_script_unparseable(tmp_path):
    script = tmp_path / "t.trigger.sh"
    _write(script, "echo hello world")
    result = await run_script(script, cwd=tmp_path)
    assert result.tag is None
    assert result.stdout.strip() == "hello world"


@pytest.mark.asyncio
async def test_run_script_non_zero_exit_fail_closed(tmp_path):
    script = tmp_path / "t.trigger.sh"
    _write(script, "echo [start]; exit 7")
    result = await run_script(script, cwd=tmp_path)
    # tag is unreachable because exit_code != 0
    assert result.tag is None
    assert result.exit_code == 7


@pytest.mark.asyncio
async def test_run_script_timeout(tmp_path, monkeypatch):
    import butterfly.session_engine.task_runner as tr
    monkeypatch.setattr(tr, "_CHECK_TIMEOUT_SEC", 0.5)
    script = tmp_path / "t.trigger.sh"
    _write(script, "sleep 5")
    result = await run_script(script, cwd=tmp_path)
    assert result.timed_out is True


@pytest.mark.asyncio
async def test_run_script_carries_message(tmp_path):
    script = tmp_path / "t.trigger.sh"
    _write(script, 'echo "[start] build ready"')
    result = await run_script(script, cwd=tmp_path)
    assert result.tag == "[start]"
    assert result.message == "build ready"


@pytest.mark.asyncio
async def test_run_script_end_marker(tmp_path):
    script = tmp_path / "t.end.sh"
    _write(script, "echo [done]")
    result = await run_script(script, cwd=tmp_path)
    assert result.tag == "[done]"


@pytest.mark.asyncio
async def test_timeout_kills_descendants(tmp_path, monkeypatch):
    """Reviewer pin (PR #45): timeout must SIGKILL the whole process group.

    A plain proc.kill() only reaps the direct bash process — a child it
    backgrounded keeps running as an orphan reparented to init. The
    script writes its grandchild's PID to a sentinel file; after the
    timeout fires we poll for that PID being gone.
    """
    import os
    import signal as _signal
    import asyncio as _asyncio

    import butterfly.session_engine.task_runner as tr
    monkeypatch.setattr(tr, "_CHECK_TIMEOUT_SEC", 0.5)
    sentinel = tmp_path / "child.pid"
    script = tmp_path / "t.trigger.sh"
    # Background a long sleep; write its PID, then hang so the
    # foreground bash gets SIGKILL'd on timeout. If killpg works the
    # backgrounded sleep dies with it.
    _write(
        script,
        f'sleep 10 & echo $! > "{sentinel}"; wait',
    )
    await run_script(script, cwd=tmp_path)
    assert sentinel.exists(), "test harness failed to capture child PID"
    child_pid = int(sentinel.read_text().strip())
    # Give the kernel a moment to reap the group.
    for _ in range(20):
        try:
            os.kill(child_pid, 0)  # probe
        except ProcessLookupError:
            return  # success — process group died with parent
        await _asyncio.sleep(0.05)
    # Best-effort cleanup if the assertion is about to fail.
    try:
        os.kill(child_pid, _signal.SIGKILL)
    except ProcessLookupError:
        pass
    raise AssertionError(
        f"child PID {child_pid} still alive after timeout — killpg did not reach descendants"
    )
