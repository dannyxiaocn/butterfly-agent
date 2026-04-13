"""Tests for nutshell chat --keep-alive functionality."""
from __future__ import annotations

import subprocess
import sys
import inspect
import tempfile
from pathlib import Path
from unittest import mock

import pytest


class TestKeepAliveParameter:
    """Tests for the keep_alive parameter on _new_session."""

    def test_new_session_has_keep_alive_param(self):
        """_new_session should accept a keep_alive keyword argument."""
        from ui.cli.chat import _new_session
        sig = inspect.signature(_new_session)
        assert "keep_alive" in sig.parameters

    def test_keep_alive_default_is_false(self):
        """The keep_alive parameter should default to False."""
        from ui.cli.chat import _new_session
        sig = inspect.signature(_new_session)
        assert sig.parameters["keep_alive"].default is False


class TestKeepAliveCLIArg:
    """Tests for --keep-alive argument in the chat subparser."""

    def _make_parser(self):
        import argparse
        from ui.cli.main import _add_chat_parser
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        _add_chat_parser(subparsers)
        return parser

    def test_keep_alive_flag_accepted(self):
        """The chat subparser should accept --keep-alive."""
        parser = self._make_parser()
        args = parser.parse_args(["chat", "--keep-alive", "hello"])
        assert args.keep_alive is True

    def test_keep_alive_default_false(self):
        """Without --keep-alive, keep_alive defaults to False."""
        parser = self._make_parser()
        args = parser.parse_args(["chat", "hello"])
        assert args.keep_alive is False

    def test_cmd_chat_passes_keep_alive_true(self):
        """cmd_chat should pass keep_alive=True to _new_session when flag is set."""
        from ui.cli.main import cmd_chat

        with mock.patch("ui.cli.chat._new_session") as mock_ns:
            mock_ns.return_value = 0
            args = mock.MagicMock()
            args.session = None
            args.entity = "agent"
            args.message = "hello"
            args.no_wait = False
            args.timeout = 10.0
            args.system_base = Path("/tmp/sys")
            args.sessions_base = Path("/tmp/sess")
            args.inject_memory = None
            args.keep_alive = True

            cmd_chat(args)

            mock_ns.assert_called_once()
            _, kwargs = mock_ns.call_args
            assert kwargs["keep_alive"] is True

    def test_cmd_chat_passes_keep_alive_false(self):
        """cmd_chat should pass keep_alive=False to _new_session when flag is not set."""
        from ui.cli.main import cmd_chat

        with mock.patch("ui.cli.chat._new_session") as mock_ns:
            mock_ns.return_value = 0
            args = mock.MagicMock()
            args.session = None
            args.entity = "agent"
            args.message = "hello"
            args.no_wait = False
            args.timeout = 10.0
            args.system_base = Path("/tmp/sys")
            args.sessions_base = Path("/tmp/sess")
            args.inject_memory = None
            args.keep_alive = False

            cmd_chat(args)

            mock_ns.assert_called_once()
            _, kwargs = mock_ns.call_args
            assert kwargs["keep_alive"] is False


class TestKeepAliveBranching:
    """Test the keep_alive branching logic inside _new_session.

    We mock all heavy dependencies so _new_session can run to the
    keep_alive branch without real infra.
    """

    def _run_new_session(self, keep_alive: bool, reply_text: str | None = "Hi"):
        """Execute _new_session with all heavy deps mocked. Returns (exit_code, popen_mock, stdout, stderr)."""
        import io
        import threading

        tmp = Path(tempfile.mkdtemp())
        session_id = "2099-01-01_00-00-00"
        system_dir = tmp / session_id
        system_dir.mkdir(parents=True)
        (system_dir / "context.jsonl").touch()

        # Also create the sessions base dir structure
        sess_dir = tmp / "sess" / session_id / "core" / "memory"
        sess_dir.mkdir(parents=True, exist_ok=True)

        fake_session = mock.MagicMock()
        fake_session.system_dir = system_dir

        popen_mock = mock.MagicMock()

        fake_loader_cls = mock.MagicMock()
        fake_loader_cls.return_value.load.return_value = mock.MagicMock()

        fake_session_cls = mock.MagicMock(return_value=fake_session)
        fake_ipc_cls = mock.MagicMock()
        fake_init_session = mock.MagicMock()

        # Pre-set event so ready_event.wait() returns True immediately
        real_event = threading.Event()
        real_event.set()

        captured_stdout = io.StringIO()
        captured_stderr = io.StringIO()

        with (
            mock.patch.dict("sys.modules", {
                "nutshell.session_engine.agent_loader": mock.MagicMock(AgentLoader=fake_loader_cls),
                "nutshell.session_engine.session": mock.MagicMock(Session=fake_session_cls),
                "nutshell.runtime.ipc": mock.MagicMock(FileIPC=fake_ipc_cls),
                "nutshell.session_engine.session_init": mock.MagicMock(init_session=fake_init_session),
            }),
            mock.patch("ui.cli.chat.datetime") as mock_dt,
            mock.patch("ui.cli.chat._send_message", return_value="fake-id"),
            mock.patch("ui.cli.chat._wait_for_reply", return_value=reply_text),
            mock.patch("ui.cli.chat._stop_daemon"),
            mock.patch("ui.cli.chat.subprocess.Popen", popen_mock),
            mock.patch("threading.Thread") as mock_thread_cls,
            mock.patch("threading.Event", return_value=real_event),
            mock.patch("sys.stdout", captured_stdout),
            mock.patch("sys.stderr", captured_stderr),
        ):
            mock_dt.now.return_value.strftime.return_value = session_id
            mock_thread_inst = mock.MagicMock()
            mock_thread_cls.return_value = mock_thread_inst

            from ui.cli.chat import _new_session
            code = _new_session(
                "agent", "hello",
                no_wait=False,
                timeout=10.0,
                system_base=tmp,
                sessions_base=tmp,
                keep_alive=keep_alive,
            )

        return code, popen_mock, captured_stdout.getvalue(), captured_stderr.getvalue()

    def test_keep_alive_true_calls_popen(self):
        """keep_alive=True should call subprocess.Popen with nutshell-server."""
        code, popen_mock, stdout, stderr = self._run_new_session(keep_alive=True)
        assert code == 0
        popen_mock.assert_called_once()
        args, kwargs = popen_mock.call_args
        assert args[0] == ["nutshell-server"]
        assert kwargs["start_new_session"] is True
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL

    def test_keep_alive_false_no_popen(self):
        """keep_alive=False should NOT call subprocess.Popen."""
        code, popen_mock, stdout, stderr = self._run_new_session(keep_alive=False)
        assert code == 0
        popen_mock.assert_not_called()

    def test_keep_alive_true_prints_message(self):
        """keep_alive=True should print the background server message."""
        code, _, stdout, stderr = self._run_new_session(keep_alive=True)
        assert "[active — server running in background]" in stdout

    def test_keep_alive_false_no_message(self):
        """keep_alive=False should NOT print the background server message."""
        code, _, stdout, stderr = self._run_new_session(keep_alive=False)
        assert "[active — server" not in stdout

    def test_keep_alive_timeout_still_launches_server(self):
        """keep_alive=True with timeout (reply=None) should still call Popen."""
        code, popen_mock, stdout, stderr = self._run_new_session(keep_alive=True, reply_text=None)
        assert code == 1
        popen_mock.assert_called_once()
        combined = stdout + stderr
        assert "[active — server running in background]" in combined
