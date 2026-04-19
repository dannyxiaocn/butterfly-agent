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
