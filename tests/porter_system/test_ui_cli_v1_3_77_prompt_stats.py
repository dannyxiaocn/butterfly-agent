"""Tests for `nutshell prompt-stats` command."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from unittest.mock import patch

from ui.cli.main import cmd_prompt_stats, _MEMORY_LAYER_INLINE_LINES


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_session(tmp_path: Path, session_id: str = "test-session") -> tuple[Path, Path]:
    """Create minimal session layout; return (sessions_base, system_base)."""
    sessions_base = tmp_path / "sessions"
    system_base = tmp_path / "_sessions"
    core = sessions_base / session_id / "core"
    core.mkdir(parents=True)
    (system_base / session_id).mkdir(parents=True)
    (system_base / session_id / "manifest.json").write_text(
        json.dumps({"id": session_id, "entity": "agent"}), encoding="utf-8"
    )
    return sessions_base, system_base


class _FakeArgs:
    def __init__(self, session_id, sessions_base, system_base):
        self.session_id = session_id
        self.sessions_base = sessions_base
        self.system_base = system_base


# ── basic tests ───────────────────────────────────────────────────────────────

def test_prompt_stats_no_sessions(tmp_path, capsys):
    args = _FakeArgs(
        session_id=None,
        sessions_base=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )
    rc = cmd_prompt_stats(args)
    assert rc == 1
    assert "No sessions" in capsys.readouterr().err


def test_prompt_stats_unknown_session(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path)
    args = _FakeArgs("nonexistent", sessions_base, system_base)
    rc = cmd_prompt_stats(args)
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_prompt_stats_empty_session(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path)
    args = _FakeArgs("test-session", sessions_base, system_base)
    rc = cmd_prompt_stats(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "prompt-stats" in out
    assert "system.md" in out
    assert "TOTAL" in out


def test_prompt_stats_with_files(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path)
    core = sessions_base / "test-session" / "core"
    (core / "system.md").write_text("You are an agent.\n" * 5)
    (core / "session.md").write_text("Session table.\n")
    (core / "memory.md").write_text("Remember this.\n")

    args = _FakeArgs("test-session", sessions_base, system_base)
    rc = cmd_prompt_stats(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "system.md" in out
    assert "memory.md" in out
    assert "STATIC" in out
    assert "DYNAMIC" in out


def test_prompt_stats_memory_layer_truncation(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path)
    core = sessions_base / "test-session" / "core"
    mem_dir = core / "memory"
    mem_dir.mkdir()
    # Write a large memory layer (>60 lines)
    big_content = "\n".join(f"line {i}" for i in range(80))
    (mem_dir / "big_layer.md").write_text(big_content)

    args = _FakeArgs("test-session", sessions_base, system_base)
    rc = cmd_prompt_stats(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "truncated" in out
    assert "memory/big_layer" in out


def test_prompt_stats_memory_layer_not_truncated(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path)
    core = sessions_base / "test-session" / "core"
    mem_dir = core / "memory"
    mem_dir.mkdir()
    small_content = "\n".join(f"line {i}" for i in range(10))
    (mem_dir / "small_layer.md").write_text(small_content)

    args = _FakeArgs("test-session", sessions_base, system_base)
    rc = cmd_prompt_stats(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "memory/small_layer" in out
    assert "truncated" not in out


def test_prompt_stats_skills(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path)
    core = sessions_base / "test-session" / "core"
    skills_dir = core / "skills"
    (skills_dir / "creator-mode").mkdir(parents=True)
    (skills_dir / "creator-mode" / "SKILL.md").write_text("# Creator Mode")

    args = _FakeArgs("test-session", sessions_base, system_base)
    rc = cmd_prompt_stats(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "skills (catalog)" in out
    assert "1 skills" in out


def test_prompt_stats_heartbeat_section(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path)
    core = sessions_base / "test-session" / "core"
    (core / "heartbeat.md").write_text("Heartbeat prompt.\n")

    args = _FakeArgs("test-session", sessions_base, system_base)
    rc = cmd_prompt_stats(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "heartbeat.md" in out
    assert "HEARTBEAT" in out


def test_prompt_stats_tokens_approximate(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path)
    core = sessions_base / "test-session" / "core"
    # 400 chars → ~100 tokens
    (core / "system.md").write_text("x" * 400)

    args = _FakeArgs("test-session", sessions_base, system_base)
    rc = cmd_prompt_stats(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "100" in out


def test_prompt_stats_default_session_skips_meta(tmp_path, capsys):
    sessions_base, system_base = _make_session(tmp_path, "agent_meta")
    meta_core = sessions_base / "agent_meta" / "core"
    (meta_core / "system.md").write_text("meta system", encoding="utf-8")

    _make_session(tmp_path, "chat-session")
    chat_core = sessions_base / "chat-session" / "core"
    (chat_core / "system.md").write_text("chat system", encoding="utf-8")

    args = _FakeArgs(None, sessions_base, system_base)

    def fake_read_all_sessions(_sessions_base, _system_base, *, exclude_meta=False):
        assert exclude_meta is True
        return [{"id": "chat-session"}]

    with patch("ui.cli.main._read_all_sessions", side_effect=fake_read_all_sessions):
        rc = cmd_prompt_stats(args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "[chat-session] prompt-stats" in out
    assert "[agent_meta] prompt-stats" not in out
