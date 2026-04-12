"""Tests for nutshell visit — agent room view."""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from ui.cli.visit import (
    _read_json,
    _read_recent_context,
    _read_tasks,
    _read_apps,
    gather_room_data,
    format_room_text,
    format_room_json,
    cmd_visit,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_session(tmp_path: Path, sid: str = "2026-01-01_00-00-00",
                  *, manifest=None, status=None, context_lines=None,
                  tasks_text=None, apps=None):
    """Create a mock session file structure under tmp_path."""
    sys_dir = tmp_path / "_sessions" / sid
    sess_dir = tmp_path / "sessions" / sid / "core"
    sys_dir.mkdir(parents=True, exist_ok=True)
    sess_dir.mkdir(parents=True, exist_ok=True)

    if manifest is not None:
        (sys_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if status is not None:
        (sys_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")
    if context_lines is not None:
        (sys_dir / "context.jsonl").write_text(
            "\n".join(json.dumps(l) for l in context_lines) + "\n",
            encoding="utf-8",
        )
    if tasks_text is not None:
        (sess_dir / "tasks.md").write_text(tasks_text, encoding="utf-8")
    if apps is not None:
        apps_dir = sess_dir / "apps"
        apps_dir.mkdir(exist_ok=True)
        for name, content in apps.items():
            (apps_dir / f"{name}.md").write_text(content, encoding="utf-8")

    return sys_dir, sess_dir


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestReadJson:
    def test_valid_json(self, tmp_path: Path):
        p = tmp_path / "test.json"
        p.write_text('{"a": 1}')
        assert _read_json(p) == {"a": 1}

    def test_missing_file(self, tmp_path: Path):
        assert _read_json(tmp_path / "nope.json") == {}

    def test_invalid_json(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text("not json")
        assert _read_json(p) == {}


class TestReadRecentContext:
    def test_missing_file(self, tmp_path: Path):
        assert _read_recent_context(tmp_path / "nope.jsonl") == []

    def test_user_input_and_turn(self, tmp_path: Path):
        lines = [
            {"type": "user_input", "content": "Hello world", "ts": "2026-01-01T00:00:00"},
            {"type": "turn", "messages": [
                {"role": "user", "content": "Hello world"},
                {"role": "assistant", "content": "Hi there, how can I help?"},
            ], "ts": "2026-01-01T00:00:01"},
        ]
        p = tmp_path / "context.jsonl"
        p.write_text("\n".join(json.dumps(l) for l in lines), encoding="utf-8")
        result = _read_recent_context(p, n=5)
        assert len(result) == 2
        assert result[0]["type"] == "user_input"
        assert result[0]["summary"] == "Hello world"
        assert result[1]["type"] == "turn"
        assert result[1]["summary"] == "Hi there, how can I help?"

    def test_truncates_to_n(self, tmp_path: Path):
        lines = []
        for i in range(10):
            lines.append({"type": "user_input", "content": f"msg {i}", "ts": f"2026-01-01T00:00:{i:02d}"})
        p = tmp_path / "context.jsonl"
        p.write_text("\n".join(json.dumps(l) for l in lines), encoding="utf-8")
        result = _read_recent_context(p, n=3)
        assert len(result) == 3
        assert result[0]["summary"] == "msg 7"

    def test_long_content_truncated(self, tmp_path: Path):
        long_text = "A" * 200
        lines = [{"type": "user_input", "content": long_text, "ts": "2026-01-01T00:00:00"}]
        p = tmp_path / "context.jsonl"
        p.write_text(json.dumps(lines[0]), encoding="utf-8")
        result = _read_recent_context(p, n=3)
        assert len(result) == 1
        assert len(result[0]["summary"]) == 80

    def test_turn_with_content_blocks(self, tmp_path: Path):
        lines = [
            {"type": "turn", "messages": [
                {"role": "assistant", "content": [
                    {"type": "text", "text": "Block reply"},
                ]},
            ], "ts": "2026-01-01T00:00:00"},
        ]
        p = tmp_path / "context.jsonl"
        p.write_text(json.dumps(lines[0]), encoding="utf-8")
        result = _read_recent_context(p, n=3)
        assert result[0]["summary"] == "Block reply"


class TestReadTasks:
    def test_existing_tasks(self, tmp_path: Path):
        p = tmp_path / "tasks.md"
        p.write_text("- [ ] Do something\n")
        assert _read_tasks(p) == "- [ ] Do something"

    def test_missing_tasks(self, tmp_path: Path):
        assert _read_tasks(tmp_path / "nope.md") == ""


class TestReadApps:
    def test_reads_md_files(self, tmp_path: Path):
        apps_dir = tmp_path / "apps"
        apps_dir.mkdir()
        (apps_dir / "weather.md").write_text("Sunny today")
        (apps_dir / "alerts.md").write_text("No alerts")
        (apps_dir / "readme.txt").write_text("ignored")
        result = _read_apps(apps_dir)
        assert result == {"alerts": "No alerts", "weather": "Sunny today"}

    def test_missing_dir(self, tmp_path: Path):
        assert _read_apps(tmp_path / "nope") == {}


class TestGatherRoomData:
    def test_full_session(self, tmp_path: Path):
        sid = "2026-01-01_00-00-00"
        _make_session(
            tmp_path, sid,
            manifest={"entity": "agent", "created_at": "2026-01-01T00:00:00"},
            status={"status": "active", "model_state": "idle", "last_run_at": "2026-01-01T00:05:00"},
            context_lines=[
                {"type": "user_input", "content": "ping", "ts": "2026-01-01T00:00:00"},
            ],
            tasks_text="- [ ] Task 1\n",
            apps={"monitor": "All OK"},
        )
        data = gather_room_data(
            sid,
            sessions_base=tmp_path / "sessions",
            system_base=tmp_path / "_sessions",
        )
        assert data["id"] == sid
        assert data["entity"] == "agent"
        assert data["status"] == "active"
        assert data["model_state"] == "idle"
        assert len(data["recent_context"]) == 1
        assert data["tasks"] == "- [ ] Task 1"
        assert data["apps"] == {"monitor": "All OK"}

    def test_missing_optional_files(self, tmp_path: Path):
        sid = "2026-01-01_00-00-00"
        _make_session(
            tmp_path, sid,
            manifest={"entity": "test"},
            status={"status": "active"},
        )
        data = gather_room_data(
            sid,
            sessions_base=tmp_path / "sessions",
            system_base=tmp_path / "_sessions",
        )
        assert data["entity"] == "test"
        assert data["recent_context"] == []
        assert data["tasks"] == ""
        assert data["apps"] == {}


class TestFormatRoomText:
    def test_output_contains_sections(self, tmp_path: Path):
        data = {
            "id": "test-session",
            "entity": "agent",
            "status": "active",
            "model_state": "idle",
            "created_at": "2026-01-01T00:00:00",
            "last_run_at": None,
            "recent_context": [
                {"type": "user_input", "summary": "Hello", "ts": None},
                {"type": "turn", "summary": "World", "ts": None},
            ],
            "tasks": "- [ ] Fix bug",
            "apps": {"weather": "Sunny"},
        }
        text = format_room_text(data)
        assert "Session: test-session" in text
        assert "Entity:  agent" in text
        assert "--- Recent Activity ---" in text
        assert ">>> Hello" in text
        assert "<<< World" in text
        assert "--- Task Board ---" in text
        assert "Fix bug" in text
        assert "--- App Notifications ---" in text
        assert "[weather]" in text
        assert "Sunny" in text

    def test_empty_data(self):
        data = {
            "id": "x",
            "entity": "?",
            "status": "unknown",
            "model_state": "unknown",
            "created_at": None,
            "last_run_at": None,
            "recent_context": [],
            "tasks": "",
            "apps": {},
        }
        text = format_room_text(data)
        assert "(no activity)" in text
        assert "(empty)" in text
        assert "App Notifications" not in text


class TestFormatRoomJson:
    def test_valid_json(self):
        data = {"id": "test", "entity": "agent", "tasks": ""}
        raw = format_room_json(data)
        parsed = json.loads(raw)
        assert parsed["id"] == "test"


class TestCmdVisit:
    def test_specific_session(self, tmp_path: Path, capsys):
        sid = "2026-01-01_00-00-00"
        _make_session(
            tmp_path, sid,
            manifest={"entity": "agent", "created_at": "2026-01-01T00:00:00"},
            status={"status": "active", "model_state": "idle"},
            tasks_text="- [ ] Todo",
        )
        args = Namespace(
            session_id=sid,
            as_json=False,
            sessions_base=tmp_path / "sessions",
            system_base=tmp_path / "_sessions",
        )
        ret = cmd_visit(args)
        assert ret == 0
        out = capsys.readouterr().out
        assert "Session: 2026-01-01_00-00-00" in out
        assert "Todo" in out

    def test_json_output(self, tmp_path: Path, capsys):
        sid = "2026-01-01_00-00-00"
        _make_session(
            tmp_path, sid,
            manifest={"entity": "agent"},
            status={"status": "active"},
        )
        args = Namespace(
            session_id=sid,
            as_json=True,
            sessions_base=tmp_path / "sessions",
            system_base=tmp_path / "_sessions",
        )
        ret = cmd_visit(args)
        assert ret == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["entity"] == "agent"

    def test_default_latest_session(self, tmp_path: Path, capsys):
        # Create two sessions; should pick the most recently active one.
        fixtures = [
            ("2026-01-01_00-00-00", "2026-01-01T00:00:00"),
            ("2026-02-01_00-00-00", "2026-02-01T00:00:00"),
        ]
        for sid, created_at in fixtures:
            _make_session(
                tmp_path, sid,
                manifest={"entity": f"ent-{sid[:7]}", "created_at": created_at},
                status={"status": "active"},
            )
        args = Namespace(
            session_id=None,
            as_json=False,
            sessions_base=tmp_path / "sessions",
            system_base=tmp_path / "_sessions",
        )
        ret = cmd_visit(args)
        assert ret == 0
        out = capsys.readouterr().out
        assert "2026-02-01_00-00-00" in out

    def test_default_session_skips_meta_when_non_meta_exists(self, tmp_path: Path, capsys):
        _make_session(
            tmp_path,
            "zz_agent_meta",
            manifest={"entity": "agent", "created_at": "2026-01-01T00:00:00"},
            status={"status": "active"},
        )
        _make_session(
            tmp_path,
            "2026-02-01_00-00-00",
            manifest={"entity": "agent", "created_at": "2026-02-01T00:00:00"},
            status={"status": "active"},
        )
        args = Namespace(
            session_id=None,
            as_json=False,
            sessions_base=tmp_path / "sessions",
            system_base=tmp_path / "_sessions",
        )
        ret = cmd_visit(args)
        assert ret == 0
        out = capsys.readouterr().out
        assert "Session: 2026-02-01_00-00-00" in out
        assert "Session: zz_agent_meta" not in out

    def test_session_not_found(self, tmp_path: Path, capsys):
        (tmp_path / "_sessions").mkdir(parents=True, exist_ok=True)
        args = Namespace(
            session_id="nonexistent",
            as_json=False,
            sessions_base=tmp_path / "sessions",
            system_base=tmp_path / "_sessions",
        )
        ret = cmd_visit(args)
        assert ret == 1
        assert "not found" in capsys.readouterr().err

    def test_no_sessions_at_all(self, tmp_path: Path, capsys):
        (tmp_path / "_sessions").mkdir(parents=True, exist_ok=True)
        args = Namespace(
            session_id=None,
            as_json=False,
            sessions_base=tmp_path / "sessions",
            system_base=tmp_path / "_sessions",
        )
        ret = cmd_visit(args)
        assert ret == 1
        assert "no sessions found" in capsys.readouterr().err
