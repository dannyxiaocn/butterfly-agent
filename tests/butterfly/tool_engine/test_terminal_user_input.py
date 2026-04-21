"""Phase 3: user-input path to the session_shell executor + queue service."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from butterfly.session_engine.terminal import TerminalLogger
from butterfly.tool_engine.executor.pure_context.session_shell import (
    SessionShellExecutor,
)
from butterfly.service.terminal_service import (
    enqueue_input,
    poll_queue,
    read_log_tail,
    read_state,
)


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_user_input_writes_and_logs_as_user_cmd(tmp_path: Path) -> None:
    logger = TerminalLogger(tmp_path / "terminal")
    ex = SessionShellExecutor(workdir=str(tmp_path), terminal_logger=logger)
    try:
        # Warm the shell with an agent command.
        await ex.execute(command="true")

        # User writes a command — include trailing newline so bash runs it.
        ok, output = await ex.user_input("echo hello-from-user\n")
        assert ok is True
        assert "hello-from-user" in output
        # Give the pty a moment to process + log.
        await asyncio.sleep(0.3)

        # Log must carry both the agent's earlier command AND the user's.
        entries = [
            line
            for line in (tmp_path / "terminal" / "log.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        assert any('"source": "agent_cmd"' in e for e in entries)
        assert any('"source": "user_cmd"' in e for e in entries)
        assert any('"source": "user_out"' in e and "hello-from-user" in e for e in entries)
    finally:
        await ex.close()


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_user_input_rejected_while_agent_holds_lock(tmp_path: Path) -> None:
    logger = TerminalLogger(tmp_path / "terminal")
    ex = SessionShellExecutor(workdir=str(tmp_path), terminal_logger=logger)
    try:
        await ex.execute(command="true")  # warm

        # Hold the agent lock with a 1s command.
        agent_task = asyncio.create_task(ex.execute(command="sleep 1", timeout=5))
        await asyncio.sleep(0.2)
        assert ex.locked_by_agent is True
        # User tries to inject — must be rejected.
        ok, out = await ex.user_input("echo ignored\n")
        assert ok is False
        assert out == ""

        await agent_task
        assert ex.locked_by_agent is False
        # Post-release user input works.
        ok, out = await ex.user_input("echo works-now\n")
        assert ok is True
        assert "works-now" in out
    finally:
        await ex.close()


def test_input_queue_round_trip(tmp_path: Path) -> None:
    """terminal_service write/read pair used by the daemon poller."""
    sessions_dir = tmp_path
    session_id = "demo"
    (sessions_dir / session_id / "core" / "terminal").mkdir(parents=True)

    id1 = enqueue_input(sessions_dir, session_id, kind="input", content="ls\n")
    id2 = enqueue_input(sessions_dir, session_id, kind="interrupt")

    queue_path = sessions_dir / session_id / "core" / "terminal" / "input.jsonl"
    entries, new_offset = poll_queue(queue_path, 0)
    assert len(entries) == 2
    assert entries[0]["id"] == id1 and entries[0]["type"] == "input"
    assert entries[1]["id"] == id2 and entries[1]["type"] == "interrupt"
    # Re-polling from the returned offset yields nothing.
    more, tail = poll_queue(queue_path, new_offset)
    assert more == []
    assert tail == new_offset


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_read_state_and_log_tail(tmp_path: Path) -> None:
    term_dir = tmp_path / "sessions" / "demo" / "core" / "terminal"
    logger = TerminalLogger(term_dir)
    ex = SessionShellExecutor(workdir=str(tmp_path), terminal_logger=logger)
    try:
        await ex.execute(command="echo tail-probe")
        state = read_state(tmp_path / "sessions", "demo")
        assert state["active"] is True
        log, size = read_log_tail(tmp_path / "sessions", "demo", limit=50)
        assert size > 0
        assert any(
            e.get("source") == "agent_out" and "tail-probe" in e.get("text", "")
            for e in log
        )
    finally:
        await ex.close()
