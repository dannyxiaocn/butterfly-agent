"""Tests for SessionWatcher — especially the pid_alive guard that prevents
competing daemons when `nutshell chat` and `nutshell server` both run."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from nutshell.session_engine.session_status import pid_alive, write_session_status


# ── pid_alive ─────────────────────────────────────────────────────────────────

def test_pid_alive_current_process():
    assert pid_alive(os.getpid()) is True


def test_pid_alive_none():
    assert pid_alive(None) is False


def test_pid_alive_zero():
    assert pid_alive(0) is False


def test_pid_alive_dead_pid():
    # PID 1 is always alive on Unix (init/launchd) but we test a clearly
    # invalid PID instead to avoid platform quirks.
    assert pid_alive(99999999) is False


# ── watcher skips sessions with a live pid ────────────────────────────────────

def _make_session(tmp_path: Path, session_id: str, pid: int | None = None, status: str = "active") -> tuple[Path, Path]:
    sys_dir = tmp_path / "_sessions" / session_id
    sys_dir.mkdir(parents=True)
    (sys_dir / "manifest.json").write_text(
        json.dumps({"entity": "agent", "created_at": "2026-01-01T00:00:00"}),
        encoding="utf-8",
    )
    write_session_status(sys_dir, status=status, pid=pid)
    ses_dir = tmp_path / "sessions" / session_id
    ses_dir.mkdir(parents=True)
    return ses_dir, sys_dir


@pytest.mark.asyncio
async def test_watcher_skips_session_with_live_pid(tmp_path):
    """Watcher must NOT start a daemon for a session whose pid is alive."""
    from nutshell.runtime.watcher import SessionWatcher

    sessions_dir = tmp_path / "sessions"
    system_dir = tmp_path / "_sessions"
    sessions_dir.mkdir()
    system_dir.mkdir()

    # Create a session whose "daemon" is our own process (pid_alive → True)
    _make_session(tmp_path, "live-session", pid=os.getpid())

    started: list[str] = []

    class TrackingWatcher(SessionWatcher):
        async def _start_session(self, session_id, sys_dir, manifest):
            started.append(session_id)

    watcher = TrackingWatcher(sessions_dir, system_dir)
    await watcher._scan()

    assert "live-session" not in started, "watcher should skip sessions with live pid"


@pytest.mark.asyncio
async def test_watcher_starts_session_with_dead_pid(tmp_path):
    """Watcher SHOULD start a daemon when pid is absent/dead."""
    from nutshell.runtime.watcher import SessionWatcher

    sessions_dir = tmp_path / "sessions"
    system_dir = tmp_path / "_sessions"
    sessions_dir.mkdir()
    system_dir.mkdir()

    _make_session(tmp_path, "dead-session", pid=None)

    started: list[str] = []

    class TrackingWatcher(SessionWatcher):
        async def _start_session(self, session_id, sys_dir, manifest):
            started.append(session_id)

    watcher = TrackingWatcher(sessions_dir, system_dir)
    await watcher._scan()
    await asyncio.sleep(0)  # let created tasks run

    assert "dead-session" in started


@pytest.mark.asyncio
async def test_watcher_skips_stopped_session(tmp_path):
    """Watcher must not restart explicitly stopped sessions."""
    from nutshell.runtime.watcher import SessionWatcher

    sessions_dir = tmp_path / "sessions"
    system_dir = tmp_path / "_sessions"
    sessions_dir.mkdir()
    system_dir.mkdir()

    _make_session(tmp_path, "stopped-session", pid=None, status="stopped")

    started: list[str] = []

    class TrackingWatcher(SessionWatcher):
        async def _start_session(self, session_id, sys_dir, manifest):
            started.append(session_id)

    watcher = TrackingWatcher(sessions_dir, system_dir)
    await watcher._scan()

    assert "stopped-session" not in started
