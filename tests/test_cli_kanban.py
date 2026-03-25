"""Tests for `nutshell kanban` — unified task board across sessions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from ui.cli.main import cmd_kanban
from ui.cli.kanban import build_kanban, format_kanban_table, format_kanban_json


# ── Helpers ───────────────────────────────────────────────────────────────────

def _seed_session(
    tmp_path: Path,
    session_id: str,
    entity: str = "agent",
    status: str = "active",
    model_state: str = "idle",
    tasks: str | None = None,
) -> tuple[Path, Path]:
    """Create minimal session + system dirs with optional tasks.md content."""
    sessions = tmp_path / "sessions"
    system = tmp_path / "_sessions"
    session_dir = sessions / session_id
    system_dir = system / session_id
    (session_dir / "core").mkdir(parents=True, exist_ok=True)
    system_dir.mkdir(parents=True, exist_ok=True)
    (system_dir / "manifest.json").write_text(
        json.dumps({"entity": entity, "created_at": "2026-03-25T10:00:00"}),
        encoding="utf-8",
    )
    (system_dir / "status.json").write_text(
        json.dumps({
            "status": status,
            "model_state": model_state,
            "pid": None,
            "last_run_at": None,
            "heartbeat_interval": 600,
        }),
        encoding="utf-8",
    )
    if tasks is not None:
        (session_dir / "core" / "tasks.md").write_text(tasks, encoding="utf-8")
    return sessions, system


def _make_args(tmp_path, session=None, as_json=False):
    return argparse.Namespace(
        session=session,
        as_json=as_json,
        sessions_base=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )


# ── build_kanban ──────────────────────────────────────────────────────────────

def test_build_kanban_with_tasks(tmp_path):
    sessions_base, _ = _seed_session(tmp_path, "s1", tasks="- [ ] task A\n- [x] task B")
    fake_sessions = [{"id": "s1", "entity": "agent", "status": "active", "model_state": "idle"}]
    entries = build_kanban(fake_sessions, sessions_base)
    assert len(entries) == 1
    assert entries[0]["id"] == "s1"
    assert entries[0]["entity"] == "agent"
    assert "task A" in entries[0]["tasks_content"]
    assert "task B" in entries[0]["tasks_content"]


def test_build_kanban_empty_tasks(tmp_path):
    sessions_base, _ = _seed_session(tmp_path, "s2", tasks="")
    fake_sessions = [{"id": "s2", "entity": "agent", "status": "active", "model_state": "idle"}]
    entries = build_kanban(fake_sessions, sessions_base)
    assert entries[0]["tasks_content"] == ""


def test_build_kanban_no_tasks_file(tmp_path):
    sessions_base, _ = _seed_session(tmp_path, "s3")  # no tasks kwarg → no file
    fake_sessions = [{"id": "s3", "entity": "agent", "status": "active", "model_state": "idle"}]
    entries = build_kanban(fake_sessions, sessions_base)
    assert entries[0]["tasks_content"] == ""


def test_build_kanban_status_classification(tmp_path):
    """Stopped sessions show as 'offline'."""
    sessions_base, _ = _seed_session(tmp_path, "stopped1", status="stopped", tasks="- task")
    fake_sessions = [{"id": "stopped1", "entity": "agent", "status": "stopped", "model_state": "idle"}]
    entries = build_kanban(fake_sessions, sessions_base)
    assert entries[0]["status"] == "offline"


def test_build_kanban_multiple_sessions(tmp_path):
    _seed_session(tmp_path, "a1", entity="agent", tasks="- task A")
    sessions_base, _ = _seed_session(tmp_path, "b1", entity="kimi", tasks="- task B")
    fake_sessions = [
        {"id": "a1", "entity": "agent", "status": "active", "model_state": "idle"},
        {"id": "b1", "entity": "kimi", "status": "active", "model_state": "idle"},
    ]
    entries = build_kanban(fake_sessions, sessions_base)
    assert len(entries) == 2
    assert entries[0]["entity"] == "agent"
    assert entries[1]["entity"] == "kimi"


# ── format_kanban_table ──────────────────────────────────────────────────────

def test_format_kanban_table_empty():
    assert format_kanban_table([]) == "No sessions found."


def test_format_kanban_table_with_content():
    entries = [
        {"id": "s1", "entity": "agent", "status": "online", "tasks_content": "- [ ] do stuff"},
        {"id": "s2", "entity": "kimi", "status": "offline", "tasks_content": ""},
    ]
    output = format_kanban_table(entries)
    assert "●" in output  # online dot
    assert "○" in output  # offline dot
    assert "agent" in output
    assert "kimi" in output
    assert "do stuff" in output
    assert "(empty)" in output


def test_format_kanban_table_multiline_tasks():
    entries = [
        {"id": "s1", "entity": "agent", "status": "idle",
         "tasks_content": "- [ ] line1\n- [x] line2"},
    ]
    output = format_kanban_table(entries)
    lines = output.splitlines()
    # Indented content lines
    assert any(line.startswith("  ") and "line1" in line for line in lines)
    assert any(line.startswith("  ") and "line2" in line for line in lines)


# ── format_kanban_json ───────────────────────────────────────────────────────

def test_format_kanban_json_valid():
    entries = [{"id": "s1", "entity": "agent", "status": "online", "tasks_content": "task"}]
    result = json.loads(format_kanban_json(entries))
    assert len(result) == 1
    assert result[0]["id"] == "s1"
    assert result[0]["tasks_content"] == "task"


# ── cmd_kanban (integration) ─────────────────────────────────────────────────

def test_cmd_kanban_all_sessions(tmp_path, capsys):
    _seed_session(tmp_path, "k1", entity="agent", tasks="- [ ] alpha")
    _seed_session(tmp_path, "k2", entity="kimi", tasks="- [ ] beta")
    args = _make_args(tmp_path)
    code = cmd_kanban(args)
    assert code == 0
    out = capsys.readouterr().out
    assert "agent" in out
    assert "kimi" in out
    assert "alpha" in out
    assert "beta" in out


def test_cmd_kanban_single_session(tmp_path, capsys):
    _seed_session(tmp_path, "k1", entity="agent", tasks="- [ ] alpha")
    _seed_session(tmp_path, "k2", entity="kimi", tasks="- [ ] beta")
    args = _make_args(tmp_path, session="k2")
    code = cmd_kanban(args)
    assert code == 0
    out = capsys.readouterr().out
    assert "kimi" in out
    assert "beta" in out
    assert "alpha" not in out


def test_cmd_kanban_session_not_found(tmp_path, capsys):
    _seed_session(tmp_path, "k1", entity="agent", tasks="stuff")
    args = _make_args(tmp_path, session="ghost")
    code = cmd_kanban(args)
    assert code == 1
    assert "not found" in capsys.readouterr().err


def test_cmd_kanban_json_output(tmp_path, capsys):
    _seed_session(tmp_path, "k1", entity="agent", tasks="- task")
    args = _make_args(tmp_path, as_json=True)
    code = cmd_kanban(args)
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    assert data[0]["id"] == "k1"
    assert data[0]["tasks_content"] == "- task"


def test_cmd_kanban_no_sessions(tmp_path, capsys):
    """Empty system — no sessions at all."""
    (tmp_path / "_sessions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
    args = _make_args(tmp_path)
    code = cmd_kanban(args)
    assert code == 0
    assert "No sessions found" in capsys.readouterr().out


def test_cmd_kanban_empty_tasks_shows_empty(tmp_path, capsys):
    _seed_session(tmp_path, "e1", entity="agent", tasks="")
    args = _make_args(tmp_path)
    code = cmd_kanban(args)
    assert code == 0
    assert "(empty)" in capsys.readouterr().out


def test_cmd_kanban_json_single_session(tmp_path, capsys):
    _seed_session(tmp_path, "j1", entity="agent", tasks="- todo")
    _seed_session(tmp_path, "j2", entity="kimi", tasks="- done")
    args = _make_args(tmp_path, session="j1", as_json=True)
    code = cmd_kanban(args)
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["id"] == "j1"
