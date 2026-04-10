"""Tests for the unified `nutshell` CLI (ui/cli/main.py)."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pytest
from porter_test_support import repo_root_from

from ui.cli.main import (
    cmd_new,
    cmd_sessions,
    cmd_stop,
    cmd_start,
    cmd_log,
    cmd_tasks,
    _add_log_parser,
    _add_prompt_stats_parser,
    _add_tasks_parser,
    _add_token_report_parser,
    _add_visit_parser,
    _read_all_sessions,
    _fmt_ago,
    _session_tone,
    _fmt_msg_content,
    _parse_inject_memory,
    _write_inject_memory,
    main,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_args(**kwargs):
    """Simple namespace factory for cmd_* functions."""
    import argparse
    from ui.cli.main import _DEFAULT_SESSIONS_BASE, _DEFAULT_SYSTEM_BASE
    defaults = {
        "sessions_base": _DEFAULT_SESSIONS_BASE,
        "system_base": _DEFAULT_SYSTEM_BASE,
        "as_json": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _seed_session(
    tmp_path: Path,
    session_id: str,
    entity: str = "agent",
    status: str = "active",
    model_state: str = "idle",
) -> tuple[Path, Path]:
    """Create minimal session + system dirs for tests."""
    sessions = tmp_path / "sessions"
    system = tmp_path / "_sessions"
    session_dir = sessions / session_id
    system_dir = system / session_id
    (session_dir / "core").mkdir(parents=True)
    system_dir.mkdir(parents=True)
    (system_dir / "manifest.json").write_text(
        json.dumps({"entity": entity, "created_at": "2026-03-25T10:00:00"}),
        encoding="utf-8",
    )
    (system_dir / "status.json").write_text(
        json.dumps({"status": status, "model_state": model_state, "pid": None,
                    "last_run_at": None, "heartbeat_interval": 600}),
        encoding="utf-8",
    )
    return sessions, system


# ── _fmt_ago ──────────────────────────────────────────────────────────────────

def test_fmt_ago_none():
    assert _fmt_ago(None) == ""


def test_fmt_ago_recent():
    from datetime import datetime, timezone, timedelta
    ts = (datetime.now(tz=timezone.utc) - timedelta(seconds=90)).isoformat()
    assert "m ago" in _fmt_ago(ts)


# ── _session_tone ─────────────────────────────────────────────────────────────

def test_session_tone_running():
    info = {"pid_alive": True, "model_state": "running", "status": "active", "has_tasks": False}
    assert _session_tone(info) == "running"

def test_session_tone_stopped():
    info = {"pid_alive": False, "model_state": "idle", "status": "stopped", "has_tasks": False}
    assert _session_tone(info) == "stopped"

def test_session_tone_idle():
    info = {"pid_alive": False, "model_state": "idle", "status": "active", "has_tasks": False}
    assert _session_tone(info) == "idle"


# ── cmd_sessions ──────────────────────────────────────────────────────────────

def test_cmd_sessions_empty(tmp_path, capsys):
    code = cmd_sessions(make_args(
        sessions_base=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    ))
    assert code == 0
    assert "No sessions" in capsys.readouterr().out


def test_cmd_sessions_table(tmp_path, capsys):
    _seed_session(tmp_path, "test-001")
    code = cmd_sessions(make_args(
        sessions_base=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    ))
    assert code == 0
    out = capsys.readouterr().out
    assert "test-001" in out
    assert "agent" in out
    assert "idle" in out


def test_cmd_sessions_json(tmp_path, capsys):
    _seed_session(tmp_path, "test-json", entity="kimi")
    code = cmd_sessions(make_args(
        sessions_base=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
        as_json=True,
    ))
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    assert data[0]["id"] == "test-json"
    assert data[0]["entity"] == "kimi"


# ── cmd_new ───────────────────────────────────────────────────────────────────

def test_cmd_new_creates_session(tmp_path, capsys):
    # Patch _DEFAULT paths via args
    import argparse
    args = argparse.Namespace(
        session_id="my-test-session",
        entity="agent",
        heartbeat=600.0,
        sessions_base=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )
    code = cmd_new(args)
    assert code == 0
    out = capsys.readouterr().out.strip()
    assert out == "my-test-session"
    assert (tmp_path / "_sessions" / "my-test-session" / "manifest.json").exists()
    assert (tmp_path / "sessions" / "my-test-session" / "core").is_dir()


def test_cmd_new_generates_id(tmp_path, capsys):
    args = argparse.Namespace(
        session_id=None,  # auto-generate
        entity="agent",
        heartbeat=600.0,
        sessions_base=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )
    code = cmd_new(args)
    assert code == 0
    out = capsys.readouterr().out.strip()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-[0-9a-f]{4}", out)
    assert (tmp_path / "_sessions" / out / "manifest.json").exists()


def test_cmd_new_bad_entity(tmp_path, capsys):
    args = argparse.Namespace(
        session_id="x",
        entity="nonexistent_entity_xyz",
        heartbeat=600.0,
        sessions_base=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )
    code = cmd_new(args)
    assert code == 1
    assert "Error" in capsys.readouterr().err


def _parse_subcommand(add_parser, argv: list[str]):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="command")
    add_parser(subparsers)
    return parser.parse_args(argv)


@pytest.mark.parametrize(
    ("add_parser", "command"),
    [
        (_add_log_parser, "log"),
        (_add_tasks_parser, "tasks"),
        (_add_prompt_stats_parser, "prompt-stats"),
        (_add_token_report_parser, "token-report"),
        (_add_visit_parser, "visit"),
    ],
)
def test_session_alias_parsers_accept_flag_form(add_parser, command):
    args = _parse_subcommand(add_parser, [command, "--session", "demo-session"])
    assert args.session_id == "demo-session"


def test_main_disables_option_abbreviation(capsys):
    with pytest.raises(SystemExit) as exc:
        from unittest.mock import patch

        with patch.object(sys, "argv", ["nutshell", "log", "--sess", "demo-session"]):
            main()

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--sess" in err
    assert ("ambiguous option" in err) or ("unrecognized arguments" in err)


# ── cmd_stop / cmd_start ──────────────────────────────────────────────────────

def test_cmd_stop_and_start(tmp_path, capsys):
    from nutshell.session_engine.session_status import read_session_status
    sessions, system = _seed_session(tmp_path, "ctrl-session")

    import argparse
    stop_args = argparse.Namespace(session_id="ctrl-session", system_base=system)
    code = cmd_stop(stop_args)
    assert code == 0
    status = read_session_status(system / "ctrl-session")
    assert status["status"] == "stopped"
    capsys.readouterr()

    start_args = argparse.Namespace(session_id="ctrl-session", system_base=system)
    code = cmd_start(start_args)
    assert code == 0
    status = read_session_status(system / "ctrl-session")
    assert status["status"] == "active"


def test_cmd_stop_not_found(tmp_path, capsys):
    import argparse
    args = argparse.Namespace(session_id="ghost-session", system_base=tmp_path / "_sessions")
    code = cmd_stop(args)
    assert code == 1
    assert "not found" in capsys.readouterr().err


def test_cmd_start_not_found(tmp_path, capsys):
    import argparse
    args = argparse.Namespace(session_id="ghost-session", system_base=tmp_path / "_sessions")
    code = cmd_start(args)
    assert code == 1
    assert "not found" in capsys.readouterr().err


# ── cmd_tasks ─────────────────────────────────────────────────────────────────

def _seed_tasks(tmp_path: Path, session_id: str, content: str = "") -> tuple[Path, Path]:
    """Seed a session with a tasks.md file."""
    sessions, system = _seed_session(tmp_path, session_id)
    tasks_path = sessions / session_id / "core" / "tasks.md"
    tasks_path.write_text(content, encoding="utf-8")
    return sessions, system


def test_cmd_tasks_shows_content(tmp_path, capsys):
    import argparse
    sessions, system = _seed_tasks(tmp_path, "task-session", "- [ ] Write tests\n- [x] Write code")
    args = argparse.Namespace(
        session_id="task-session",
        sessions_base=sessions,
        system_base=system,
    )
    code = cmd_tasks(args)
    assert code == 0
    out = capsys.readouterr().out
    assert "task-session" in out
    assert "Write tests" in out
    assert "Write code" in out


def test_cmd_tasks_empty(tmp_path, capsys):
    import argparse
    sessions, system = _seed_tasks(tmp_path, "empty-session", "")
    args = argparse.Namespace(
        session_id="empty-session",
        sessions_base=sessions,
        system_base=system,
    )
    code = cmd_tasks(args)
    assert code == 0
    assert "(empty)" in capsys.readouterr().out


def test_cmd_tasks_no_tasks_file(tmp_path, capsys):
    """Session exists but tasks.md was never written."""
    import argparse
    _, system = _seed_session(tmp_path, "notask-session")
    args = argparse.Namespace(
        session_id="notask-session",
        sessions_base=tmp_path / "sessions",
        system_base=system,
    )
    code = cmd_tasks(args)
    assert code == 0
    assert "empty" in capsys.readouterr().out.lower()


def test_cmd_tasks_not_found(tmp_path, capsys):
    import argparse
    args = argparse.Namespace(
        session_id="ghost",
        sessions_base=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )
    code = cmd_tasks(args)
    assert code == 1
    assert "not found" in capsys.readouterr().err


def test_cmd_tasks_defaults_to_latest(tmp_path, capsys):
    """With no session_id given, picks the most recently active session."""
    import argparse
    # Create two sessions; _read_all_sessions returns most-recent first
    sessions, system = _seed_tasks(tmp_path, "latest-session", "- [ ] Top priority task")
    _seed_session(tmp_path, "older-session")
    args = argparse.Namespace(
        session_id=None,
        sessions_base=sessions,
        system_base=system,
    )
    code = cmd_tasks(args)
    assert code == 0
    out = capsys.readouterr().out
    # Output should reference one of the sessions (whichever is "latest")
    assert "session" in out


# ── _fmt_msg_content ──────────────────────────────────────────────────────────

def test_fmt_msg_content_str():
    assert _fmt_msg_content("hello") == "hello"


def test_fmt_msg_content_list_text():
    content = [{"type": "text", "text": "hi there"}]
    assert _fmt_msg_content(content) == "hi there"


def test_fmt_msg_content_list_tool_use():
    content = [{"type": "tool_use", "name": "bash", "input": {"command": "pwd"}}]
    result = _fmt_msg_content(content)
    assert "tool: bash" in result
    assert "pwd" in result


def test_fmt_msg_content_list_mixed():
    content = [
        {"type": "tool_use", "name": "bash", "input": {}},
        {"type": "text", "text": "done"},
    ]
    result = _fmt_msg_content(content)
    assert "tool: bash" in result
    assert "done" in result


# ── cmd_log ───────────────────────────────────────────────────────────────────

def _seed_context(tmp_path: Path, session_id: str, events: list) -> tuple[Path, Path]:
    """Seed a session with context.jsonl events."""
    import json as _json
    sessions, system = _seed_session(tmp_path, session_id)
    context_path = system / session_id / "context.jsonl"
    context_path.write_text(
        "\n".join(_json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )
    return sessions, system


def test_cmd_log_basic(tmp_path, capsys):
    import argparse
    events = [
        {"type": "user_input", "content": "say hello", "id": "uid-1", "ts": "2026-03-25T10:00:00"},
        {"type": "turn", "triggered_by": "user",
         "messages": [
             {"role": "user", "content": "say hello", "ts": "2026-03-25T10:00:01"},
             {"role": "assistant", "content": "Hello!", "ts": "2026-03-25T10:00:02"},
         ],
         "user_input_id": "uid-1",
         "usage": {"input": 100, "output": 5, "cache_read": 0, "cache_write": 0},
         "ts": "2026-03-25T10:00:02"},
    ]
    sessions, system = _seed_context(tmp_path, "log-session", events)
    args = argparse.Namespace(
        session_id="log-session",
        num_turns=5,
        sessions_base=sessions,
        system_base=system,
    )
    code = cmd_log(args)
    assert code == 0
    out = capsys.readouterr().out
    assert "say hello" in out
    assert "Hello!" in out
    assert "↑100" in out
    assert "↓5" in out


def test_cmd_log_no_history(tmp_path, capsys):
    import argparse
    _, system = _seed_session(tmp_path, "no-log-session")
    args = argparse.Namespace(
        session_id="no-log-session",
        num_turns=5,
        sessions_base=tmp_path / "sessions",
        system_base=system,
    )
    code = cmd_log(args)
    assert code == 0
    assert "No conversation" in capsys.readouterr().out


def test_cmd_log_not_found(tmp_path, capsys):
    import argparse
    args = argparse.Namespace(
        session_id="ghost",
        num_turns=5,
        sessions_base=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )
    code = cmd_log(args)
    assert code == 1
    assert "not found" in capsys.readouterr().err


def test_cmd_log_limits_turns(tmp_path, capsys):
    import argparse

    def _make_turn(n):
        return [
            {"type": "user_input", "content": f"msg{n}", "id": f"uid-{n}", "ts": "2026-03-25T10:00:00"},
            {"type": "turn", "triggered_by": "user",
             "messages": [{"role": "assistant", "content": f"reply{n}", "ts": "2026-03-25T10:00:01"}],
             "user_input_id": f"uid-{n}", "ts": "2026-03-25T10:00:01"},
        ]

    events = []
    for i in range(1, 6):
        events.extend(_make_turn(i))

    sessions, system = _seed_context(tmp_path, "limit-session", events)
    args = argparse.Namespace(
        session_id="limit-session",
        num_turns=2,
        sessions_base=sessions,
        system_base=system,
    )
    code = cmd_log(args)
    assert code == 0
    out = capsys.readouterr().out
    assert "reply4" in out
    assert "reply5" in out
    assert "reply1" not in out


# ── inject-memory helpers ─────────────────────────────────────────────────────

def test_parse_inject_memory_empty():
    assert _parse_inject_memory(None) == {}
    assert _parse_inject_memory([]) == {}


def test_parse_inject_memory_value():
    result = _parse_inject_memory(["foo=bar", "x=hello world"])
    assert result == {"foo": "bar", "x": "hello world"}


def test_parse_inject_memory_file(tmp_path):
    f = tmp_path / "content.md"
    f.write_text("# Track\n- item")
    result = _parse_inject_memory([f"track=@{f}"])
    assert result == {"track": "# Track\n- item"}


def test_parse_inject_memory_bad_format():
    with pytest.raises(SystemExit):
        _parse_inject_memory(["noequals"])


def test_parse_inject_memory_missing_file():
    with pytest.raises(SystemExit):
        _parse_inject_memory(["key=@/nonexistent/path/file.md"])


def test_write_inject_memory(tmp_path):
    session_dir = tmp_path / "sessions" / "test-session"
    _write_inject_memory(session_dir, {"foo": "bar content", "baz": "qux"})
    assert (session_dir / "core" / "memory" / "foo.md").read_text() == "bar content"
    assert (session_dir / "core" / "memory" / "baz.md").read_text() == "qux"


def test_write_inject_memory_overwrites(tmp_path):
    session_dir = tmp_path / "s"
    mem_dir = session_dir / "core" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "key.md").write_text("old")
    _write_inject_memory(session_dir, {"key": "new"})
    assert (mem_dir / "key.md").read_text() == "new"


def test_cmd_new_inject_memory(tmp_path):
    """cmd_new with --inject-memory writes memory layers after session creation."""
    import argparse

    sessions = tmp_path / "sessions"
    system = tmp_path / "_sessions"

    assert (repo_root_from(Path(__file__)) / "entity" / "agent").exists()
    args = argparse.Namespace(
        session_id="inject-test",
        entity="agent",
        heartbeat=600.0,
        sessions_base=sessions,
        system_base=system,
        inject_memory=["mykey=myvalue", "other=content here"],
    )
    from ui.cli.main import cmd_new
    import unittest.mock as mock
    with mock.patch("nutshell.session_engine.session_init.init_session") as m:
        m.return_value = None
        # cmd_new calls _write_inject_memory after init_session
        # We need session dir to exist for mkdir to work on memory
        (sessions / "inject-test" / "core" / "memory").mkdir(parents=True)
        code = cmd_new(args)
    assert code == 0
    assert (sessions / "inject-test" / "core" / "memory" / "mykey.md").read_text() == "myvalue"
    assert (sessions / "inject-test" / "core" / "memory" / "other.md").read_text() == "content here"
