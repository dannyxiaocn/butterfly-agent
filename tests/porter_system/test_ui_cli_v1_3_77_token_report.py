"""Tests for `nutshell token-report` command."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ui.cli.main import cmd_token_report


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_session(tmp_path: Path, session_id: str = "test-sess") -> tuple[Path, Path]:
    sessions_base = tmp_path / "sessions"
    system_base = tmp_path / "_sessions"
    (sessions_base / session_id).mkdir(parents=True)
    (system_base / session_id).mkdir(parents=True)
    (system_base / session_id / "manifest.json").write_text(
        json.dumps({"id": session_id, "entity": "agent"}), encoding="utf-8"
    )
    return sessions_base, system_base


def _write_context(system_base: Path, session_id: str, events: list[dict]) -> None:
    path = system_base / session_id / "context.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")


class _Args:
    def __init__(self, session_id, sessions_base, system_base):
        self.session_id = session_id
        self.sessions_base = sessions_base
        self.system_base = system_base


# ── tests ──────────────────────────────────────────────────────────────────────

def test_no_sessions(tmp_path, capsys):
    args = _Args(None, tmp_path / "sessions", tmp_path / "_sessions")
    rc = cmd_token_report(args)
    assert rc == 1
    assert "No sessions" in capsys.readouterr().err


def test_unknown_session(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path)
    args = _Args("nonexistent", sessions_base, system_base)
    rc = cmd_token_report(args)
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_no_history(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path)
    args = _Args("test-sess", sessions_base, system_base)
    rc = cmd_token_report(args)
    assert rc == 0
    assert "No conversation" in capsys.readouterr().out


def test_turns_without_usage(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path)
    _write_context(system_base, "test-sess", [
        {"type": "turn", "messages": [], "ts": "2026-01-01T00:00:00"},
    ])
    args = _Args("test-sess", sessions_base, system_base)
    rc = cmd_token_report(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "token-report" in out
    assert "1 turn" in out


def test_turns_with_usage(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path)
    _write_context(system_base, "test-sess", [
        {"type": "user_input", "id": "u1", "content": "hello world", "ts": "2026-01-01T10:00:00"},
        {
            "type": "turn", "user_input_id": "u1",
            "messages": [],
            "ts": "2026-01-01T10:00:01",
            "usage": {"input": 1000, "output": 200, "cache_read": 500, "cache_write": 0},
        },
    ])
    args = _Args("test-sess", sessions_base, system_base)
    rc = cmd_token_report(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "1000" in out
    assert "200" in out
    assert "500" in out
    assert "hello world" in out


def test_totals_row(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path)
    _write_context(system_base, "test-sess", [
        {
            "type": "turn", "messages": [], "ts": "2026-01-01T10:00:00",
            "usage": {"input": 1000, "output": 100},
        },
        {
            "type": "turn", "messages": [], "ts": "2026-01-01T10:01:00",
            "usage": {"input": 2000, "output": 200},
        },
    ])
    args = _Args("test-sess", sessions_base, system_base)
    rc = cmd_token_report(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "TOT" in out
    assert "3000" in out   # total input
    assert "300" in out    # total output


def test_cache_efficiency_shown(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path)
    _write_context(system_base, "test-sess", [
        {
            "type": "turn", "messages": [], "ts": "2026-01-01T10:00:00",
            "usage": {"input": 1000, "output": 100, "cache_read": 3000, "cache_write": 1000},
        },
    ])
    args = _Args("test-sess", sessions_base, system_base)
    rc = cmd_token_report(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Cache hit rate" in out
    assert "%" in out


def test_most_expensive_turns(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path)
    events = []
    for i in range(5):
        events.append({
            "type": "turn", "messages": [], "ts": f"2026-01-01T10:0{i}:00",
            "usage": {"input": (i + 1) * 1000, "output": 100},
        })
    _write_context(system_base, "test-sess", events)
    args = _Args("test-sess", sessions_base, system_base)
    rc = cmd_token_report(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Most expensive" in out


def test_heartbeat_trigger_label(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path)
    _write_context(system_base, "test-sess", [
        {
            "type": "turn", "messages": [], "ts": "2026-01-01T10:00:00",
            "pre_triggered": True,
            "usage": {"input": 500, "output": 50},
        },
    ])
    args = _Args("test-sess", sessions_base, system_base)
    rc = cmd_token_report(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "[heartbeat]" in out


def test_trigger_truncated_at_40_chars(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path)
    long_msg = "x" * 80
    _write_context(system_base, "test-sess", [
        {"type": "user_input", "id": "u1", "content": long_msg, "ts": "2026-01-01T10:00:00"},
        {
            "type": "turn", "user_input_id": "u1", "messages": [],
            "ts": "2026-01-01T10:00:01",
            "usage": {"input": 100, "output": 10},
        },
    ])
    args = _Args("test-sess", sessions_base, system_base)
    rc = cmd_token_report(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "…" in out
