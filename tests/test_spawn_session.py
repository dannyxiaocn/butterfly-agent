"""Tests for spawn_session tool and session_factory."""
from __future__ import annotations

import json
import pytest
from pathlib import Path

from nutshell.runtime.session_factory import init_session


# ── session_factory.init_session ──────────────────────────────────────────────

def test_init_session_creates_directory_structure(tmp_path):
    init_session(
        session_id="test-session",
        entity_name="nonexistent-entity",  # graceful fallback when entity missing
        sessions_base=tmp_path / "sessions",
        system_sessions_base=tmp_path / "_sessions",
        entity_base=tmp_path / "entity",
    )

    session_dir = tmp_path / "sessions" / "test-session"
    system_dir = tmp_path / "_sessions" / "test-session"

    assert (session_dir / "core").is_dir()
    assert (session_dir / "core" / "tools").is_dir()
    assert (session_dir / "core" / "skills").is_dir()
    assert (session_dir / "docs").is_dir()
    assert (session_dir / "playground").is_dir()
    assert (system_dir / "manifest.json").exists()
    assert (system_dir / "context.jsonl").exists()
    assert (system_dir / "events.jsonl").exists()


def test_init_session_writes_manifest(tmp_path):
    init_session(
        session_id="my-session",
        entity_name="agent",
        sessions_base=tmp_path / "sessions",
        system_sessions_base=tmp_path / "_sessions",
    )

    manifest = json.loads(
        (tmp_path / "_sessions" / "my-session" / "manifest.json").read_text()
    )
    assert manifest["session_id"] == "my-session"
    assert manifest["entity"] == "agent"
    assert "created_at" in manifest


def test_init_session_writes_initial_message(tmp_path):
    init_session(
        session_id="chat-session",
        entity_name="agent",
        sessions_base=tmp_path / "sessions",
        system_sessions_base=tmp_path / "_sessions",
        initial_message="Hello, please do X",
    )

    ctx = tmp_path / "_sessions" / "chat-session" / "context.jsonl"
    events = [json.loads(l) for l in ctx.read_text().splitlines() if l.strip()]
    assert len(events) == 1
    assert events[0]["type"] == "user_input"
    assert events[0]["content"] == "Hello, please do X"
    assert "id" in events[0]


def test_init_session_no_initial_message_empty_context(tmp_path):
    init_session(
        session_id="quiet-session",
        entity_name="agent",
        sessions_base=tmp_path / "sessions",
        system_sessions_base=tmp_path / "_sessions",
    )

    ctx = tmp_path / "_sessions" / "quiet-session" / "context.jsonl"
    assert ctx.read_text().strip() == ""


def test_init_session_idempotent(tmp_path):
    kwargs = dict(
        session_id="idem-session",
        entity_name="agent",
        sessions_base=tmp_path / "sessions",
        system_sessions_base=tmp_path / "_sessions",
    )
    init_session(**kwargs)
    init_session(**kwargs)  # second call should not raise or overwrite core files

    session_dir = tmp_path / "sessions" / "idem-session"
    assert (session_dir / "core").is_dir()


# ── spawn_session tool ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_spawn_session_creates_session(tmp_path):
    from nutshell.tool_engine.providers.spawn_session import spawn_session

    result = await spawn_session(
        entity="agent",
        _sessions_base=tmp_path / "sessions",
        _system_sessions_base=tmp_path / "_sessions",
        _entity_base=tmp_path / "entity",
    )

    assert "created" in result.lower() or "session" in result.lower()
    # At least one session directory should exist
    sessions = list((tmp_path / "_sessions").iterdir())
    assert len(sessions) == 1
    assert (sessions[0] / "manifest.json").exists()


@pytest.mark.asyncio
async def test_spawn_session_with_initial_message(tmp_path):
    from nutshell.tool_engine.providers.spawn_session import spawn_session

    await spawn_session(
        entity="agent",
        initial_message="Start the analysis",
        _sessions_base=tmp_path / "sessions",
        _system_sessions_base=tmp_path / "_sessions",
        _entity_base=tmp_path / "entity",
    )

    sessions = list((tmp_path / "_sessions").iterdir())
    ctx = sessions[0] / "context.jsonl"
    events = [json.loads(l) for l in ctx.read_text().splitlines() if l.strip()]
    assert any(e["content"] == "Start the analysis" for e in events)


# ── registry integration ──────────────────────────────────────────────────────

def test_spawn_session_registered_as_builtin():
    from nutshell.tool_engine.registry import get_builtin
    impl = get_builtin("spawn_session")
    assert impl is not None
    assert callable(impl)
