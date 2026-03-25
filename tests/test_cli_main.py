"""Tests for the unified `nutshell` CLI (ui/cli/main.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ui.cli.main import (
    cmd_new,
    cmd_sessions,
    cmd_stop,
    cmd_start,
    _read_all_sessions,
    _fmt_ago,
    _session_tone,
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
    entity_base = Path(__file__).parent.parent / "entity"
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
    import argparse
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
    assert len(out) > 0  # some ID printed
    assert (tmp_path / "_sessions" / out / "manifest.json").exists()


def test_cmd_new_bad_entity(tmp_path, capsys):
    import argparse
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


# ── cmd_stop / cmd_start ──────────────────────────────────────────────────────

def test_cmd_stop_and_start(tmp_path, capsys):
    from nutshell.runtime.status import read_session_status
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
