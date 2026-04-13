"""Tests for v1.3.86 server PID file helpers and CLI parsing."""
from __future__ import annotations

import os
import argparse
from unittest import mock

import pytest

from nutshell.runtime.server import (
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
    with mock.patch("sys.argv", ["nutshell-server"]):
        with mock.patch("nutshell.runtime.server._cmd_start", return_value=0) as m:
            with mock.patch("sys.exit") as exit_mock:
                main()
                m.assert_called_once()
                exit_mock.assert_called_once_with(0)


def test_main_stop_subcommand():
    with mock.patch("sys.argv", ["nutshell-server", "stop"]):
        with mock.patch("nutshell.runtime.server._cmd_stop", return_value=0) as m:
            with mock.patch("sys.exit"):
                main()
                m.assert_called_once()


def test_main_status_subcommand():
    with mock.patch("sys.argv", ["nutshell-server", "status"]):
        with mock.patch("nutshell.runtime.server._cmd_status", return_value=0) as m:
            with mock.patch("sys.exit"):
                main()
                m.assert_called_once()


def test_main_update_subcommand():
    with mock.patch("sys.argv", ["nutshell-server", "update"]):
        with mock.patch("nutshell.runtime.server._cmd_update", return_value=0) as m:
            with mock.patch("sys.exit"):
                main()
                m.assert_called_once()


def test_main_foreground_flag_without_subcommand():
    """nutshell-server --foreground should call _cmd_start with foreground=True."""
    with mock.patch("sys.argv", ["nutshell-server", "--foreground"]):
        with mock.patch("nutshell.runtime.server._cmd_start", return_value=0) as m:
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
