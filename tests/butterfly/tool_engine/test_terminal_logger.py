"""Phase 2: `TerminalLogger` captures shell I/O + state through
`TerminalExecutor`."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from butterfly.session_engine.terminal import TerminalLogger
from butterfly.tool_engine.executor.pure_context.terminal import (
    TerminalExecutor,
)


def _read_log(term_dir: Path) -> list[dict]:
    text = (term_dir / "log.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_logger_records_agent_cmd_and_output(tmp_path: Path) -> None:
    logger = TerminalLogger(tmp_path / "terminal")
    ex = TerminalExecutor(workdir=str(tmp_path), terminal_logger=logger)
    try:
        await ex.create()
        out = await ex.use(command="echo hello-from-agent")
        assert "hello-from-agent" in out

        entries = _read_log(logger.directory)
        sources = [e["source"] for e in entries]

        # Must see: shell lifecycle system notice + agent_cmd + at least
        # one agent_out chunk carrying the echo.
        assert "system" in sources, sources
        assert "agent_cmd" in sources, sources
        assert any(e["source"] == "agent_out" for e in entries), sources
        assert any(
            e["source"] == "agent_out" and "hello-from-agent" in e["text"]
            for e in entries
        ), entries
    finally:
        await ex.close()


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_state_json_tracks_lifecycle_and_lock(tmp_path: Path) -> None:
    logger = TerminalLogger(tmp_path / "terminal")
    ex = TerminalExecutor(workdir=str(tmp_path), terminal_logger=logger)
    try:
        await ex.create()
        await ex.use(command="true")
        state = logger.read_state()
        assert state["active"] is True
        assert isinstance(state["shell_pid"], int) and state["shell_pid"] > 0
        # After use returns the agent lock is released.
        assert state["locked_by"] is None
        assert state["last_active_at"] is not None
    finally:
        await ex.close()


def test_seq_is_monotonic_and_survives_restart(tmp_path: Path) -> None:
    """Every append_* stamp increments `seq`; a fresh TerminalLogger against
    the same directory resumes numbering from the existing line count so
    log.jsonl is a total-order single source of truth."""
    dir1 = tmp_path / "terminal"
    t1 = TerminalLogger(dir1)
    t1.append_system("first")
    t1.append_command("agent_cmd", "pwd")
    t1.append_output("agent_out", "/tmp\n")

    lines = (dir1 / "log.jsonl").read_text(encoding="utf-8").splitlines()
    entries = [json.loads(s) for s in lines if s]
    assert [e["seq"] for e in entries] == [0, 1, 2]

    # Simulate a daemon restart — fresh logger must continue the sequence,
    # not restart at 0 (which would collide with existing ids).
    t2 = TerminalLogger(dir1)
    t2.append_command("user_cmd", "ls")
    lines2 = (dir1 / "log.jsonl").read_text(encoding="utf-8").splitlines()
    entries2 = [json.loads(s) for s in lines2 if s]
    assert [e["seq"] for e in entries2] == [0, 1, 2, 3]


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_locked_by_agent_while_executing(tmp_path: Path) -> None:
    import asyncio

    logger = TerminalLogger(tmp_path / "terminal")
    ex = TerminalExecutor(workdir=str(tmp_path), terminal_logger=logger)
    try:
        # Warm the shell so spawn cost isn't in the timing window.
        await ex.create()
        await ex.use(command="true")

        # Start a 1-second command; peek state mid-flight.
        task = asyncio.create_task(ex.use(command="sleep 1", timeout=5))
        await asyncio.sleep(0.2)
        mid = logger.read_state()
        assert mid["locked_by"] == "agent", mid
        await task
        end = logger.read_state()
        assert end["locked_by"] is None
    finally:
        await ex.close()
