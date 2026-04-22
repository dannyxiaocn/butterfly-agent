"""Tests for v1.3.86 server PID file helpers and CLI parsing."""
from __future__ import annotations

import os
import argparse
from unittest import mock

import pytest

from butterfly.runtime.server import (
    _write_pid,
    _read_pid,
    _clear_pid,
    _is_server_running,
    _pid_file,
    _log_file,
    _cmd_status,
    _cmd_stop,
    _system_dir_from_args,
    main,
)


# ── _pid_file / _log_file ───────────────────────────────────────────────────


def test_pid_file_default():
    """Default _pid_file uses _SYSTEM_SESSIONS_DIR."""
    pf = _pid_file()
    assert pf.name == "server.pid"


def test_pid_file_custom(tmp_path):
    assert _pid_file(tmp_path) == tmp_path / "server.pid"


def test_log_file_custom(tmp_path):
    assert _log_file(tmp_path) == tmp_path / "server.log"


# ── PID file helpers (with system_dir) ───────────────────────────────────────


def test_write_and_read_pid(tmp_path):
    """_write_pid writes current PID; _read_pid reads it back."""
    _write_pid(tmp_path)
    assert (tmp_path / "server.pid").exists()
    assert _read_pid(tmp_path) == os.getpid()


def test_read_pid_no_file(tmp_path):
    assert _read_pid(tmp_path) is None


def test_read_pid_invalid_content(tmp_path):
    (tmp_path / "server.pid").write_text("not-a-number")
    assert _read_pid(tmp_path) is None


def test_clear_pid(tmp_path):
    (tmp_path / "server.pid").write_text("12345")
    _clear_pid(tmp_path)
    assert not (tmp_path / "server.pid").exists()


def test_clear_pid_missing_file(tmp_path):
    """_clear_pid is safe when file doesn't exist."""
    _clear_pid(tmp_path)  # should not raise


def test_is_server_running_no_pid_file(tmp_path):
    assert _is_server_running(tmp_path) is None


def test_is_server_running_stale_pid(tmp_path):
    """Stale PID (process not running) returns None and cleans up."""
    (tmp_path / "server.pid").write_text("999999999")
    result = _is_server_running(tmp_path)
    assert result is None
    assert not (tmp_path / "server.pid").exists()  # stale PID cleaned up


def test_is_server_running_with_current_process(tmp_path):
    """Current process PID should be detected as running."""
    (tmp_path / "server.pid").write_text(str(os.getpid()))
    result = _is_server_running(tmp_path)
    assert result == os.getpid()


# ── _system_dir_from_args ────────────────────────────────────────────────────


def test_system_dir_from_args_with_attr(tmp_path):
    args = argparse.Namespace(system_sessions_dir=str(tmp_path))
    assert _system_dir_from_args(args) == tmp_path


def test_system_dir_from_args_without_attr():
    """Falls back to default when attr missing."""
    args = argparse.Namespace()
    result = _system_dir_from_args(args)
    assert result.name == "_sessions"


# ── CLI parsing (dict-based dispatch) ────────────────────────────────────────


def test_main_defaults_to_start():
    """With no subcommand, main() defaults to 'start' via _COMMANDS[None]."""
    with mock.patch("sys.argv", ["butterfly-server"]):
        with mock.patch("butterfly.runtime.server._cmd_start", return_value=0) as m:
            with mock.patch("sys.exit") as exit_mock:
                main()
                m.assert_called_once()
                exit_mock.assert_called_once_with(0)


def test_main_stop_subcommand():
    with mock.patch("sys.argv", ["butterfly-server", "stop"]):
        with mock.patch("butterfly.runtime.server._cmd_stop", return_value=0) as m:
            with mock.patch("sys.exit"):
                main()
                m.assert_called_once()


def test_main_status_subcommand():
    with mock.patch("sys.argv", ["butterfly-server", "status"]):
        with mock.patch("butterfly.runtime.server._cmd_status", return_value=0) as m:
            with mock.patch("sys.exit"):
                main()
                m.assert_called_once()


def test_main_foreground_flag_without_subcommand():
    """butterfly-server --foreground should call _cmd_start with foreground=True."""
    with mock.patch("sys.argv", ["butterfly-server", "--foreground"]):
        with mock.patch("butterfly.runtime.server._cmd_start", return_value=0) as m:
            with mock.patch("sys.exit"):
                main()
                args = m.call_args[0][0]
                assert args.foreground is True


# ── _cmd_status ──────────────────────────────────────────────────────────────


def test_cmd_status_not_running(tmp_path, capsys):
    args = argparse.Namespace(system_sessions_dir=str(tmp_path))
    result = _cmd_status(args)
    assert result == 0
    assert "not running" in capsys.readouterr().out


def test_cmd_status_running(tmp_path, capsys):
    (tmp_path / "server.pid").write_text(str(os.getpid()))
    args = argparse.Namespace(system_sessions_dir=str(tmp_path))
    result = _cmd_status(args)
    assert result == 0
    assert "running" in capsys.readouterr().out


# ── _cmd_stop ────────────────────────────────────────────────────────────────


def test_cmd_stop_not_running(tmp_path, capsys):
    args = argparse.Namespace(system_sessions_dir=str(tmp_path))
    result = _cmd_stop(args)
    assert result == 0
    assert "not running" in capsys.readouterr().out


# ── flock-based startup mutex (v2.0.18) ──────────────────────────────────────
#
# Guards against two butterfly daemons running against the same
# ``system_sessions_dir``. The PID file alone doesn't catch orphan
# daemons (parent died, detached daemon kept running, untracked by
# ``server.pid``) — the flock does, because it's kernel-enforced and
# auto-released only when the holding process actually exits.


def test_acquire_exclusive_lock_creates_lock_file(tmp_path):
    from butterfly.runtime.server import (
        _acquire_exclusive_lock,
        _lock_file,
        _release_lock,
    )
    try:
        assert _acquire_exclusive_lock(tmp_path) is True
        assert _lock_file(tmp_path).exists()
    finally:
        _release_lock()


def test_acquire_exclusive_lock_is_idempotent_in_same_process(tmp_path):
    """Second call in the same process short-circuits to True without
    re-flocking — avoids 'already locked' errors when a code path calls
    it twice (defensive; shouldn't happen in practice)."""
    from butterfly.runtime.server import _acquire_exclusive_lock, _release_lock
    try:
        assert _acquire_exclusive_lock(tmp_path) is True
        assert _acquire_exclusive_lock(tmp_path) is True  # no exception
    finally:
        _release_lock()


def test_acquire_exclusive_lock_fails_when_held_by_another_fd(tmp_path):
    """Simulates a second butterfly process by holding the flock from a
    separate ``open()`` — ``_acquire_exclusive_lock`` must return False
    without raising. Different file descriptions on the same file trigger
    BSD flock contention (verified on macOS + Linux)."""
    import fcntl

    from butterfly.runtime.server import (
        _acquire_exclusive_lock,
        _lock_file,
        _release_lock,
    )

    # Pre-grab the lock via an independent fd (another "process" for the
    # purposes of flock).
    path = _lock_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    blocker = open(path, "a")
    fcntl.flock(blocker.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        try:
            assert _acquire_exclusive_lock(tmp_path) is False
        finally:
            _release_lock()
    finally:
        fcntl.flock(blocker.fileno(), fcntl.LOCK_UN)
        blocker.close()


def test_release_lock_lets_another_acquire(tmp_path):
    from butterfly.runtime.server import _acquire_exclusive_lock, _release_lock

    try:
        assert _acquire_exclusive_lock(tmp_path) is True
    finally:
        _release_lock()

    # After release, a fresh acquire must succeed.
    try:
        assert _acquire_exclusive_lock(tmp_path) is True
    finally:
        _release_lock()


# ── _run singleton guard end-to-end ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_refuses_to_start_when_lock_is_held(tmp_path, capsys):
    """v2.0.18: ``_run`` must bail out without starting a watcher or
    writing ``server.pid`` when the singleton lock is already held.
    Previously two daemons could race on the shared ``_sessions/`` dir
    (observed: duplicate ``thinking_start`` events + two ``turn``
    entries per ``user_input`` on session 2026-04-17_20-42-18-2917).
    """
    import fcntl

    from butterfly.runtime import server as server_mod

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Pre-grab the lock from an independent fd — mimics a live first
    # butterfly daemon holding it.
    lock_path = server_mod._lock_file(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    blocker = open(lock_path, "a")
    fcntl.flock(blocker.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        await server_mod._run(sessions_dir, tmp_path)
    finally:
        fcntl.flock(blocker.fileno(), fcntl.LOCK_UN)
        blocker.close()

    # _run must have early-returned without writing ``server.pid``.
    assert not (tmp_path / "server.pid").exists()
    err = capsys.readouterr().err
    assert "already running" in err
    assert "Refusing to start a second instance" in err


@pytest.mark.asyncio
async def test_run_refuses_even_for_orphan_daemon_without_pid_file(tmp_path, capsys):
    """Orphan daemon scenario: lock is held by a live (unrelated) process
    but ``server.pid`` was cleaned up (parent-CLI exit path cleared it, or
    the orphan's original ``_write_pid`` was overwritten). The flock still
    catches it — the error message surfaces 'pid unknown — orphan' so the
    user knows to look at ``ps`` instead of relying on ``server.pid``.
    """
    import fcntl

    from butterfly.runtime import server as server_mod

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # Hold the lock, but intentionally DO NOT create server.pid.
    lock_path = server_mod._lock_file(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    blocker = open(lock_path, "a")
    fcntl.flock(blocker.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        await server_mod._run(sessions_dir, tmp_path)
    finally:
        fcntl.flock(blocker.fileno(), fcntl.LOCK_UN)
        blocker.close()

    err = capsys.readouterr().err
    assert "already running" in err
    assert "orphan" in err


# ── Orphan-daemon scanner (v2.0.18) ──────────────────────────────────────────

def test_scan_butterfly_daemons_skips_self(tmp_path):
    """The scanner must exclude the current process. Otherwise running
    ``_cmd_server_stop`` from within a test that matches the command
    string would SIGTERM the test runner."""
    from butterfly.runtime.server import _scan_butterfly_daemons
    # Whatever the test runner's command line is, scanning tmp_path must
    # not include ``os.getpid()`` — the scanner explicitly skips ``my_pid``.
    result = _scan_butterfly_daemons(tmp_path)
    assert os.getpid() not in result


def test_scan_butterfly_daemons_returns_empty_for_unused_dir(tmp_path):
    """No butterfly.runtime.server process is running against this fresh
    tmp_path, so scan must return []."""
    from butterfly.runtime.server import _scan_butterfly_daemons
    assert _scan_butterfly_daemons(tmp_path) == []


def test_scan_butterfly_daemons_matches_system_dir_in_command(tmp_path):
    """Integration-ish: when ``ps`` output contains a real
    ``butterfly.runtime.server --system-sessions-dir <tmp_path>`` line
    (simulated via a stub ``subprocess.run``), the scanner returns the
    matched PIDs and filters out lines that don't mention the dir."""
    from unittest import mock

    from butterfly.runtime import server as server_mod

    fake_output = (
        f"12345 python -m butterfly.runtime.server --foreground "
        f"--sessions-dir /x --system-sessions-dir {tmp_path}\n"
        f"67890 python -m butterfly.runtime.server --foreground "
        f"--sessions-dir /x --system-sessions-dir /some/other/dir\n"
        f"99999 python -m butterfly.runtime.server --foreground "
        f"--sessions-dir /y --system-sessions-dir {tmp_path}\n"
        f"11111 python some-other-program\n"
    )

    class _R:
        returncode = 0
        stdout = fake_output

    with mock.patch.object(server_mod.subprocess, "run", return_value=_R()):
        result = server_mod._scan_butterfly_daemons(tmp_path)

    # Matches only PIDs whose command line references our tmp_path AND
    # mentions butterfly.runtime.server.
    assert sorted(result) == [12345, 99999]


# ── _run singleton guard end-to-end ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_proceeds_when_lock_is_free(tmp_path, monkeypatch):
    """Fresh start (no other server holding the lock): ``_run`` acquires,
    calls ``_write_pid``, spins up the (mocked) watcher, and cleans up on
    exit. The PID file must be gone AFTER ``_run`` returns (finally block
    runs ``_clear_pid``), and the lock must be released so a subsequent
    ``_run`` can re-acquire.
    """
    from butterfly.runtime import server as server_mod

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    class _NoopWatcher:
        def __init__(self, *a, **kw):
            pass

        async def run(self, stop_event):
            return

    import butterfly.runtime.watcher as watcher_mod
    monkeypatch.setattr(watcher_mod, "SessionWatcher", _NoopWatcher)

    await server_mod._run(sessions_dir, tmp_path)

    # Finally cleared the pid and released the lock — a second run can
    # re-acquire without conflict.
    assert not (tmp_path / "server.pid").exists()
    assert server_mod._acquire_exclusive_lock(tmp_path) is True
    server_mod._release_lock()
