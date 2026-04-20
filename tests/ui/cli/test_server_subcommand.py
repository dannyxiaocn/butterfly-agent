"""v2.0.18: ``butterfly server`` became a subcommand group
(tail / status / stop). These tests pin the argparse wiring so
``butterfly server stop`` dispatches to the stop handler, ``butterfly
server status`` dispatches to status, and bare ``butterfly server``
keeps the v2.0.16 tail-the-log behaviour (UX: existing muscle memory
doesn't break).
"""
from __future__ import annotations

from unittest import mock

from ui.cli import main as cli_main


def _build_parser():
    """Replicate the parser build that ``main()`` does, without running
    it. Lets us parse an argv and inspect the resulting Namespace."""
    import argparse
    parser = argparse.ArgumentParser(prog="butterfly", allow_abbrev=False)
    sub = parser.add_subparsers(dest="cmd")
    cli_main._add_server_parser(sub)
    return parser


def test_server_default_action_is_tail():
    """``butterfly server`` with no sub-sub-command routes to tail.

    Preserves v2.0.16 behaviour: users with shell aliases / muscle memory
    still get the log tail when they type ``butterfly server`` alone.
    """
    parser = _build_parser()
    args = parser.parse_args(["server"])
    with mock.patch.object(cli_main, "_cmd_server_tail", return_value=0) as m_tail, \
         mock.patch.object(cli_main, "_cmd_server_stop", return_value=0) as m_stop, \
         mock.patch.object(cli_main, "_cmd_server_status", return_value=0) as m_status:
        cli_main.cmd_server(args)
    m_tail.assert_called_once()
    m_stop.assert_not_called()
    m_status.assert_not_called()


def test_server_tail_explicit_routes_to_tail():
    parser = _build_parser()
    args = parser.parse_args(["server", "tail"])
    with mock.patch.object(cli_main, "_cmd_server_tail", return_value=0) as m_tail:
        cli_main.cmd_server(args)
    m_tail.assert_called_once()


def test_server_status_routes_to_status():
    parser = _build_parser()
    args = parser.parse_args(["server", "status"])
    with mock.patch.object(cli_main, "_cmd_server_status", return_value=0) as m_status:
        cli_main.cmd_server(args)
    m_status.assert_called_once()


def test_server_stop_routes_to_stop():
    """The main fix from this PR: ``butterfly server stop`` must reach
    ``_cmd_server_stop`` (user-reported 2026-04-17: they tried
    ``butterfly stop`` which is a session-level command and errored
    ``session_id required``). The new subcommand gives them an ergonomic
    route to stop the daemon + orphan sweep."""
    parser = _build_parser()
    args = parser.parse_args(["server", "stop"])
    with mock.patch.object(cli_main, "_cmd_server_stop", return_value=0) as m_stop:
        cli_main.cmd_server(args)
    m_stop.assert_called_once()


def test_server_stop_kills_tracked_and_orphans(tmp_path, capsys):
    """``_cmd_server_stop`` must SIGTERM the tracked daemon (via the
    runtime's ``_cmd_stop``) AND sweep any orphan butterfly daemons
    surfaced by ``_scan_butterfly_daemons``. Without the orphan sweep,
    the reason this feature exists — cleaning up the zombie daemon that
    inspired the 2026-04-17 bug report — is unresolved.

    v2.0.30 also sweeps any foreground butterfly shell holding the
    web-UI port via ``_scan_port_holders``; mocked to [] here so this
    test still pins the daemon + orphan sweep in isolation.
    """
    import signal

    with mock.patch.object(cli_main, "_DEFAULT_SYSTEM_BASE", tmp_path), \
         mock.patch("butterfly.runtime.server._is_server_running", return_value=12345), \
         mock.patch("butterfly.runtime.server._scan_butterfly_daemons",
                    return_value=[12345, 67890, 99999]), \
         mock.patch.object(cli_main, "_scan_port_holders", return_value=[]), \
         mock.patch("butterfly.runtime.server._cmd_stop", return_value=0) as m_cmd_stop, \
         mock.patch.object(cli_main.os, "kill") as m_kill, \
         mock.patch.object(cli_main.time, "sleep", return_value=None):
        cli_main._cmd_server_stop(None)

    # Tracked daemon: SIGTERM via runtime's graceful _cmd_stop.
    m_cmd_stop.assert_called_once()

    # Orphans (12345 is tracked, so 67890 + 99999 are orphans): SIGTERM
    # them directly, then the verify-alive loop also does ``kill(pid, 0)``.
    term_calls = [
        c for c in m_kill.call_args_list
        if c.args and c.args[1] == signal.SIGTERM
    ]
    term_pids = sorted(c.args[0] for c in term_calls)
    assert term_pids == [67890, 99999]

    # The verification pass calls kill(pid, 0) for each orphan to see if
    # they survived — it raises ProcessLookupError to mark them as
    # already-dead; mock.kill swallows that path, so we just assert the
    # stdout mentioned both PIDs.
    out = capsys.readouterr().out
    assert "67890" in out
    assert "99999" in out


def test_server_stop_kills_foreground_web_port_holder(tmp_path, capsys):
    """v2.0.30: the foreground ``butterfly`` (no-args) shell runs uvicorn
    in-process and is never listed in server.pid; the orphan-daemon scan
    can't see it. The ``butterfly server stop`` one-click must include
    it, so port-hold detection feeds into the SIGTERM sweep."""
    import signal

    with mock.patch.object(cli_main, "_DEFAULT_SYSTEM_BASE", tmp_path), \
         mock.patch("butterfly.runtime.server._is_server_running", return_value=None), \
         mock.patch("butterfly.runtime.server._scan_butterfly_daemons", return_value=[]), \
         mock.patch.object(cli_main, "_scan_port_holders", return_value=[42424]), \
         mock.patch("butterfly.runtime.server._cmd_stop") as m_cmd_stop, \
         mock.patch.object(cli_main.os, "kill") as m_kill, \
         mock.patch.object(cli_main.time, "sleep", return_value=None):
        cli_main._cmd_server_stop(None)
    # No tracked daemon, so runtime._cmd_stop is never invoked.
    m_cmd_stop.assert_not_called()
    # The foreground web holder IS sent SIGTERM.
    term_calls = [
        c for c in m_kill.call_args_list
        if c.args and c.args[1] == signal.SIGTERM
    ]
    assert [c.args[0] for c in term_calls] == [42424]


def test_server_stop_prints_helpful_message_when_nothing_running(tmp_path, capsys):
    with mock.patch.object(cli_main, "_DEFAULT_SYSTEM_BASE", tmp_path), \
         mock.patch("butterfly.runtime.server._is_server_running", return_value=None), \
         mock.patch("butterfly.runtime.server._scan_butterfly_daemons", return_value=[]), \
         mock.patch.object(cli_main, "_scan_port_holders", return_value=[]), \
         mock.patch("butterfly.runtime.server._cmd_stop") as m_cmd_stop, \
         mock.patch.object(cli_main.os, "kill") as m_kill, \
         mock.patch.object(cli_main.time, "sleep", return_value=None):
        cli_main._cmd_server_stop(None)
    # Neither the tracked-kill path nor the orphan-kill path runs.
    m_cmd_stop.assert_not_called()
    m_kill.assert_not_called()
    assert "nothing to stop" in capsys.readouterr().out
