"""Phase 5: idle close + cwd restore for the persistent session_shell."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from butterfly.session_engine.terminal import TerminalLogger
from butterfly.tool_engine.executor.pure_context.session_shell import (
    SessionShellExecutor,
)


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_snapshot_and_close_writes_cwd(tmp_path: Path) -> None:
    logger = TerminalLogger(tmp_path / "terminal")
    ex = SessionShellExecutor(workdir=str(tmp_path), terminal_logger=logger)
    try:
        await ex.execute(command="cd /tmp")
        did = await ex.snapshot_and_close()
        assert did is True
        assert ex.shell.is_alive() is False
        snap = json.loads((tmp_path / "terminal" / "snapshot.json").read_text(encoding="utf-8"))
        assert snap["cwd"] == "/tmp"
        assert isinstance(snap["ts"], (int, float)) and snap["ts"] > 0
    finally:
        await ex.close()


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_respawn_restores_cwd(tmp_path: Path) -> None:
    logger = TerminalLogger(tmp_path / "terminal")
    ex = SessionShellExecutor(workdir=str(tmp_path), terminal_logger=logger)
    try:
        await ex.execute(command="cd /tmp")
        await ex.snapshot_and_close()

        # Next execute triggers a respawn; _maybe_restore should cd back.
        out = await ex.execute(command="pwd")
        assert "/tmp" in out
    finally:
        await ex.close()


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_maybe_idle_close_respects_threshold(tmp_path: Path) -> None:
    logger = TerminalLogger(tmp_path / "terminal")
    ex = SessionShellExecutor(workdir=str(tmp_path), terminal_logger=logger)
    try:
        await ex.execute(command="true")
        # Threshold not exceeded — no-op.
        assert await ex.maybe_idle_close(threshold=3600.0) is False
        assert ex.shell.is_alive() is True
        # With threshold=0, any past activity qualifies as idle.
        assert await ex.maybe_idle_close(threshold=0.0) is True
        assert ex.shell.is_alive() is False
    finally:
        await ex.close()
