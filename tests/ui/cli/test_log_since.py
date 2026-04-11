"""Tests for nutshell log --since / --watch helpers (v1.3.36)."""

import json
import time
import types
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ui.cli.main import _parse_since, _turn_ts, _load_context, cmd_log


# ── _parse_since ──────────────────────────────────────────────────────────────

class TestParseSince:
    def test_now_returns_current_time(self):
        before = time.time()
        result = _parse_since("now")
        after = time.time()
        assert before <= result <= after

    def test_iso8601_datetime(self):
        result = _parse_since("2026-03-25T12:00:00")
        expected = datetime(2026, 3, 25, 12, 0, 0).timestamp()
        assert result == expected

    def test_iso8601_date_only(self):
        result = _parse_since("2026-03-25")
        expected = datetime(2026, 3, 25).timestamp()
        assert result == expected

    def test_unix_timestamp_string(self):
        result = _parse_since("1742900400")
        assert result == 1742900400.0

    def test_unix_timestamp_float_string(self):
        result = _parse_since("1742900400.5")
        assert result == 1742900400.5

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            _parse_since("yesterday")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            _parse_since("")

    def test_small_number_raises(self):
        """Numbers < 1 billion are rejected (not a plausible UNIX epoch)."""
        with pytest.raises(ValueError, match="Cannot parse"):
            _parse_since("12345")

    def test_negative_number_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            _parse_since("-1")


# ── _turn_ts ──────────────────────────────────────────────────────────────────

class TestTurnTs:
    def test_valid_iso_ts(self):
        turn = {"type": "turn", "ts": "2026-03-25T14:30:00"}
        result = _turn_ts(turn)
        assert result == datetime(2026, 3, 25, 14, 30, 0).timestamp()

    def test_missing_ts_returns_none(self):
        assert _turn_ts({"type": "turn"}) is None

    def test_none_ts_returns_none(self):
        assert _turn_ts({"type": "turn", "ts": None}) is None

    def test_invalid_ts_returns_none(self):
        assert _turn_ts({"type": "turn", "ts": "not-a-date"}) is None

    def test_ts_with_microseconds(self):
        turn = {"ts": "2026-03-25T14:30:00.123456"}
        result = _turn_ts(turn)
        assert result is not None
        assert abs(result - datetime(2026, 3, 25, 14, 30, 0, 123456).timestamp()) < 0.001


# ── _load_context ─────────────────────────────────────────────────────────────

class TestLoadContext:
    def test_empty_file(self, tmp_path):
        p = tmp_path / "context.jsonl"
        p.write_text("")
        inputs, turns = _load_context(p)
        assert inputs == {}
        assert turns == []

    def test_user_input_and_turn(self, tmp_path):
        p = tmp_path / "context.jsonl"
        lines = [
            json.dumps({"type": "user_input", "id": "u1", "content": "hello", "ts": "2026-03-25T10:00:00"}),
            json.dumps({"type": "turn", "user_input_id": "u1", "ts": "2026-03-25T10:00:05", "messages": []}),
        ]
        p.write_text("\n".join(lines) + "\n")
        inputs, turns = _load_context(p)
        assert "u1" in inputs
        assert len(turns) == 1
        assert turns[0]["user_input_id"] == "u1"

    def test_malformed_json_skipped(self, tmp_path):
        p = tmp_path / "context.jsonl"
        lines = [
            "NOT VALID JSON",
            json.dumps({"type": "turn", "ts": "2026-03-25T10:00:00", "messages": []}),
        ]
        p.write_text("\n".join(lines) + "\n")
        inputs, turns = _load_context(p)
        assert len(turns) == 1

    def test_blank_lines_ignored(self, tmp_path):
        p = tmp_path / "context.jsonl"
        lines = [
            "",
            json.dumps({"type": "user_input", "id": "u1", "content": "hi", "ts": "2026-03-25T10:00:00"}),
            "   ",
            json.dumps({"type": "turn", "ts": "2026-03-25T10:01:00", "messages": []}),
            "",
        ]
        p.write_text("\n".join(lines))
        inputs, turns = _load_context(p)
        assert len(inputs) == 1
        assert len(turns) == 1


# ── cmd_log with --since ──────────────────────────────────────────────────────

def _make_args(tmp_path, since=None, watch=False, num_turns=5, session_id="test-session"):
    """Build a namespace that looks like argparse output for cmd_log."""
    system_base = tmp_path / "_sessions"
    sessions_base = tmp_path / "sessions"
    system_base.mkdir(parents=True)
    sessions_base.mkdir(parents=True)

    sess_dir = system_base / session_id
    sess_dir.mkdir()
    # Write a minimal manifest
    (sess_dir / "manifest.json").write_text(json.dumps({"entity": "agent"}))

    return types.SimpleNamespace(
        session_id=session_id,
        num_turns=num_turns,
        since=since,
        watch=watch,
        system_base=system_base,
        sessions_base=sessions_base,
    ), sess_dir


def _write_context(sess_dir, events):
    """Write a list of event dicts as context.jsonl."""
    lines = [json.dumps(ev) for ev in events]
    (sess_dir / "context.jsonl").write_text("\n".join(lines) + "\n")


class TestCmdLogSince:
    def test_since_filters_old_turns(self, tmp_path, capsys):
        args, sess_dir = _make_args(tmp_path, since="2026-03-25T12:00:00")
        _write_context(sess_dir, [
            {"type": "user_input", "id": "u1", "content": "old msg", "ts": "2026-03-25T08:00:00"},
            {"type": "turn", "user_input_id": "u1", "ts": "2026-03-25T08:00:05", "messages": [
                {"role": "assistant", "content": "old reply"}
            ]},
            {"type": "user_input", "id": "u2", "content": "new msg", "ts": "2026-03-25T14:00:00"},
            {"type": "turn", "user_input_id": "u2", "ts": "2026-03-25T14:00:05", "messages": [
                {"role": "assistant", "content": "new reply"}
            ]},
        ])
        rc = cmd_log(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "new reply" in out
        assert "old reply" not in out

    def test_since_no_matching_turns(self, tmp_path, capsys):
        args, sess_dir = _make_args(tmp_path, since="2026-12-31T00:00:00")
        _write_context(sess_dir, [
            {"type": "user_input", "id": "u1", "content": "hi", "ts": "2026-03-25T08:00:00"},
            {"type": "turn", "user_input_id": "u1", "ts": "2026-03-25T08:00:05", "messages": []},
        ])
        rc = cmd_log(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "No new turns" in out

    def test_since_shows_all_matching_ignores_n(self, tmp_path, capsys):
        """When --since is set, -n is ignored and ALL matching turns are shown."""
        args, sess_dir = _make_args(tmp_path, since="2026-03-25T00:00:00", num_turns=1)
        events = [
            {"type": "user_input", "id": "u1", "content": "msg1", "ts": "2026-03-25T10:00:00"},
            {"type": "turn", "user_input_id": "u1", "ts": "2026-03-25T10:00:05", "messages": [
                {"role": "assistant", "content": "reply1"}
            ]},
            {"type": "user_input", "id": "u2", "content": "msg2", "ts": "2026-03-25T11:00:00"},
            {"type": "turn", "user_input_id": "u2", "ts": "2026-03-25T11:00:05", "messages": [
                {"role": "assistant", "content": "reply2"}
            ]},
            {"type": "user_input", "id": "u3", "content": "msg3", "ts": "2026-03-25T12:00:00"},
            {"type": "turn", "user_input_id": "u3", "ts": "2026-03-25T12:00:05", "messages": [
                {"role": "assistant", "content": "reply3"}
            ]},
        ]
        _write_context(sess_dir, events)
        rc = cmd_log(args)
        assert rc == 0
        out = capsys.readouterr().out
        # All 3 should appear even though -n=1
        assert "reply1" in out
        assert "reply2" in out
        assert "reply3" in out

    def test_without_since_respects_n(self, tmp_path, capsys):
        """Without --since, -n limits the output."""
        args, sess_dir = _make_args(tmp_path, since=None, num_turns=1)
        events = [
            {"type": "user_input", "id": "u1", "content": "msg1", "ts": "2026-03-25T10:00:00"},
            {"type": "turn", "user_input_id": "u1", "ts": "2026-03-25T10:00:05", "messages": [
                {"role": "assistant", "content": "reply1"}
            ]},
            {"type": "user_input", "id": "u2", "content": "msg2", "ts": "2026-03-25T11:00:00"},
            {"type": "turn", "user_input_id": "u2", "ts": "2026-03-25T11:00:05", "messages": [
                {"role": "assistant", "content": "reply2"}
            ]},
        ]
        _write_context(sess_dir, events)
        rc = cmd_log(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "reply2" in out
        assert "reply1" not in out

    def test_since_with_unix_timestamp(self, tmp_path, capsys):
        cutoff = datetime(2026, 3, 25, 12, 0, 0).timestamp()
        args, sess_dir = _make_args(tmp_path, since=str(cutoff))
        _write_context(sess_dir, [
            {"type": "turn", "ts": "2026-03-25T08:00:00", "messages": [
                {"role": "assistant", "content": "before"}
            ]},
            {"type": "turn", "ts": "2026-03-25T14:00:00", "messages": [
                {"role": "assistant", "content": "after"}
            ]},
        ])
        rc = cmd_log(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "after" in out
        assert "before" not in out

    def test_missing_context_file(self, tmp_path, capsys):
        """Session exists but no context.jsonl → friendly message."""
        args, sess_dir = _make_args(tmp_path, since=None)
        # Don't write context.jsonl
        rc = cmd_log(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "No conversation history" in out

    def test_session_not_found(self, tmp_path, capsys):
        """Non-existent session → error."""
        args, sess_dir = _make_args(tmp_path, session_id="nonexistent")
        # Remove the manifest we auto-created
        (sess_dir / "manifest.json").unlink()
        sess_dir.rmdir()
        args.session_id = "does-not-exist"
        rc = cmd_log(args)
        assert rc == 1
        err = capsys.readouterr().err
        assert "not found" in err
