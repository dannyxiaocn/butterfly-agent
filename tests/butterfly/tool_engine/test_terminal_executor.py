"""Persistent TerminalExecutor — `terminal_create` + `terminal_use`."""
from __future__ import annotations

import sys

import pytest

from butterfly.tool_engine.executor.pure_context.terminal import TerminalExecutor


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_terminal_persists_cwd(tmp_path) -> None:
    ex = TerminalExecutor(workdir=str(tmp_path))
    try:
        await ex.create()
        r1 = await ex.use(command="cd /tmp")
        assert "[exit 0" in r1
        r2 = await ex.use(command="pwd")
        assert "/tmp" in r2
        assert "[exit 0" in r2
    finally:
        await ex.close()


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_terminal_persists_env(tmp_path) -> None:
    ex = TerminalExecutor(workdir=str(tmp_path))
    try:
        await ex.create()
        r1 = await ex.use(command="export BFY_TEST_PERSIST=hello_there")
        assert "[exit 0" in r1
        r2 = await ex.use(command="echo $BFY_TEST_PERSIST")
        assert "hello_there" in r2
    finally:
        await ex.close()


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_terminal_captures_nonzero_exit(tmp_path) -> None:
    ex = TerminalExecutor(workdir=str(tmp_path))
    try:
        await ex.create()
        # Run in a subshell so `exit 7` doesn't kill the persistent shell itself.
        out = await ex.use(command="(exit 7)", timeout=5)
        assert "[exit 7" in out
    finally:
        await ex.close()


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_terminal_timeout_recovers(tmp_path) -> None:
    ex = TerminalExecutor(workdir=str(tmp_path))
    try:
        await ex.create()
        # sleep 10 with a 1s timeout — should be interrupted by ^C.
        out = await ex.use(command="sleep 10", timeout=1)
        assert "timed out after 1" in out
        # Next call should succeed (shell either survived the SIGINT or was restarted).
        out2 = await ex.use(command="echo after", timeout=5)
        assert "after" in out2
        assert "[exit 0" in out2
    finally:
        await ex.close()


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_terminal_timeout_zero_regression(tmp_path) -> None:
    """Cubic P2 (confirmed): `timeout=0` is silently replaced with the
    default 60 s. ``float(kwargs.get("timeout") or default)`` treats 0
    as falsy — the caller's explicit "time out immediately" is ignored.
    Documented via xfail until the kwargs parser swaps in an explicit
    ``None`` check."""
    ex = TerminalExecutor(workdir=str(tmp_path))
    try:
        await ex.create()
        # With a true zero timeout, the command should time out immediately.
        # The bug makes it fall back to 60s so `sleep 2` actually succeeds.
        out = await ex.use(command="sleep 2", timeout=0)
        if "[exit 0" in out:
            pytest.xfail(
                "TerminalExecutor swaps timeout=0 for default (cubic P2, not fixed)."
            )
        else:
            assert "timed out" in out
    finally:
        await ex.close()


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_terminal_create_is_idempotent(tmp_path) -> None:
    """Calling ``terminal_create`` twice on a live shell is a no-op on
    the process (the existing bash stays up) and just re-reports the
    current env fingerprint — the `is_created` flag replaces the old
    ``reset=true`` footgun."""
    ex = TerminalExecutor(workdir=str(tmp_path))
    try:
        welcome_a = await ex.create()
        pid_a = ex.shell._proc.pid if ex.shell._proc is not None else None
        welcome_b = await ex.create()
        pid_b = ex.shell._proc.pid if ex.shell._proc is not None else None
        assert pid_a == pid_b
        # Welcome text is rendered from the fingerprint — both calls
        # against the same cwd yield an identical block.
        assert welcome_a == welcome_b
    finally:
        await ex.close()
