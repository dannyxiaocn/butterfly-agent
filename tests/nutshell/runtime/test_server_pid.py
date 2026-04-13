"""Tests for v1.3.86 server PID file helpers and CLI parsing."""
from __future__ import annotations

import os
import argparse
from unittest import mock

import pytest


# ── PID file helpers ─────────────────────────────────────────────────────────


def test_write_and_read_pid(tmp_path):
    """_write_pid writes current PID; _read_pid reads it back."""
    from nutshell.runtime import server
    pid_file = tmp_path / "server.pid"
    with mock.patch.object(server, "_PID_FILE", pid_file):
        server._write_pid()
        assert pid_file.exists()
        assert server._read_pid() == os.getpid()


def test_read_pid_no_file(tmp_path):
    from nutshell.runtime import server
    with mock.patch.object(server, "_PID_FILE", tmp_path / "nope.pid"):
        assert server._read_pid() is None


def test_read_pid_invalid_content(tmp_path):
    from nutshell.runtime import server
    pid_file = tmp_path / "server.pid"
    pid_file.write_text("not-a-number")
    with mock.patch.object(server, "_PID_FILE", pid_file):
        assert server._read_pid() is None


def test_clear_pid(tmp_path):
    from nutshell.runtime import server
    pid_file = tmp_path / "server.pid"
    pid_file.write_text("12345")
    with mock.patch.object(server, "_PID_FILE", pid_file):
        server._clear_pid()
        assert not pid_file.exists()


def test_clear_pid_missing_file(tmp_path):
    """_clear_pid is safe when file doesn't exist."""
    from nutshell.runtime import server
    with mock.patch.object(server, "_PID_FILE", tmp_path / "nope.pid"):
        server._clear_pid()  # should not raise


def test_is_server_running_no_pid_file(tmp_path):
    from nutshell.runtime import server
    with mock.patch.object(server, "_PID_FILE", tmp_path / "nope.pid"):
        assert server._is_server_running() is None


def test_is_server_running_stale_pid(tmp_path):
    """Stale PID (process not running) returns None and cleans up."""
    from nutshell.runtime import server
    pid_file = tmp_path / "server.pid"
    pid_file.write_text("999999999")  # very unlikely to be running
    with mock.patch.object(server, "_PID_FILE", pid_file):
        result = server._is_server_running()
        assert result is None
        assert not pid_file.exists()  # stale PID cleaned up


def test_is_server_running_with_current_process(tmp_path):
    """Current process PID should be detected as running."""
    from nutshell.runtime import server
    pid_file = tmp_path / "server.pid"
    pid_file.write_text(str(os.getpid()))
    with mock.patch.object(server, "_PID_FILE", pid_file):
        result = server._is_server_running()
        assert result == os.getpid()


# ── CLI parsing ──────────────────────────────────────────────────────────────


def test_main_defaults_to_start():
    """With no subcommand, main() defaults to 'start'."""
    from nutshell.runtime.server import main
    with mock.patch("sys.argv", ["nutshell-server"]):
        with mock.patch("nutshell.runtime.server._cmd_start", return_value=0) as m:
            with mock.patch("sys.exit") as exit_mock:
                main()
                m.assert_called_once()
                exit_mock.assert_called_once_with(0)


def test_main_stop_subcommand():
    from nutshell.runtime.server import main
    with mock.patch("sys.argv", ["nutshell-server", "stop"]):
        with mock.patch("nutshell.runtime.server._cmd_stop", return_value=0) as m:
            with mock.patch("sys.exit"):
                main()
                m.assert_called_once()


def test_main_status_subcommand():
    from nutshell.runtime.server import main
    with mock.patch("sys.argv", ["nutshell-server", "status"]):
        with mock.patch("nutshell.runtime.server._cmd_status", return_value=0) as m:
            with mock.patch("sys.exit"):
                main()
                m.assert_called_once()


# ── _cmd_status ──────────────────────────────────────────────────────────────


def test_cmd_status_not_running(tmp_path, capsys):
    from nutshell.runtime import server
    with mock.patch.object(server, "_PID_FILE", tmp_path / "nope.pid"):
        result = server._cmd_status(argparse.Namespace())
        assert result == 0
        assert "not running" in capsys.readouterr().out


def test_cmd_status_running(tmp_path, capsys):
    from nutshell.runtime import server
    pid_file = tmp_path / "server.pid"
    pid_file.write_text(str(os.getpid()))
    with mock.patch.object(server, "_PID_FILE", pid_file):
        result = server._cmd_status(argparse.Namespace())
        assert result == 0
        assert "running" in capsys.readouterr().out


# ── _cmd_stop ────────────────────────────────────────────────────────────────


def test_cmd_stop_not_running(tmp_path, capsys):
    from nutshell.runtime import server
    with mock.patch.object(server, "_PID_FILE", tmp_path / "nope.pid"):
        result = server._cmd_stop(argparse.Namespace())
        assert result == 0
        assert "not running" in capsys.readouterr().out
