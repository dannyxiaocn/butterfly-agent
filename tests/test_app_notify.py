"""Tests for app_notify built-in tool and app-notifications system prompt injection."""
from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from nutshell.core.agent import Agent
from nutshell.core.provider import Provider
from nutshell.core.types import AgentResult, TokenUsage


# ── Helpers ────────────────────────────────────────────────────────


class MockProvider(Provider):
    def __init__(self, responses):
        self._responses = iter(responses)

    async def complete(self, messages, tools, system_prompt, model, *,
                       on_text_chunk=None, cache_system_prefix="",
                       cache_last_human_turn=False, thinking: bool = False, thinking_budget: int = 8000, thinking_effort: str = "high"):
        r = next(self._responses)
        return (r[0], r[1], r[2] if len(r) > 2 else TokenUsage())


def _make_session(tmp_path, sid="test-app"):
    """Create a minimal Session for testing."""
    from nutshell.runtime.session import Session
    provider = MockProvider([("ok", [], TokenUsage())])
    agent = Agent(provider=provider)
    return Session(
        agent,
        session_id=sid,
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )


# ── Tool: app_notify ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_app_notify_no_session(tmp_path):
    """Returns error when NUTSHELL_SESSION_ID is not set."""
    from nutshell.tool_engine.providers.app_notify import app_notify

    with patch.dict("os.environ", {}, clear=True):
        result = await app_notify(action="list", _sessions_base=tmp_path)
    assert "Error" in result
    assert "NUTSHELL_SESSION_ID" in result


@pytest.mark.asyncio
async def test_app_notify_list_empty(tmp_path):
    """list returns 'No app notifications' when apps/ does not exist."""
    from nutshell.tool_engine.providers.app_notify import app_notify

    sessions_base = tmp_path / "sessions"
    sid = "test-session"
    (sessions_base / sid / "core").mkdir(parents=True)

    with patch.dict("os.environ", {"NUTSHELL_SESSION_ID": sid}):
        result = await app_notify(action="list", _sessions_base=sessions_base)
    assert "No app notifications" in result


@pytest.mark.asyncio
async def test_app_notify_write_and_list(tmp_path):
    """write creates a file; list shows it."""
    from nutshell.tool_engine.providers.app_notify import app_notify

    sessions_base = tmp_path / "sessions"
    sid = "test-session"
    (sessions_base / sid / "core").mkdir(parents=True)

    with patch.dict("os.environ", {"NUTSHELL_SESSION_ID": sid}):
        result = await app_notify(action="write", app="weather", content="Sunny 25°C", _sessions_base=sessions_base)
        assert "Written" in result
        assert "weather.md" in result

        # File exists on disk
        f = sessions_base / sid / "core" / "apps" / "weather.md"
        assert f.exists()
        assert f.read_text() == "Sunny 25°C"

        # list shows it
        result = await app_notify(action="list", _sessions_base=sessions_base)
        assert "weather" in result
        assert "1" in result  # count


@pytest.mark.asyncio
async def test_app_notify_write_requires_app_and_content(tmp_path):
    """write without app or content returns error."""
    from nutshell.tool_engine.providers.app_notify import app_notify

    sessions_base = tmp_path / "sessions"
    sid = "test-session"
    (sessions_base / sid / "core").mkdir(parents=True)

    with patch.dict("os.environ", {"NUTSHELL_SESSION_ID": sid}):
        r1 = await app_notify(action="write", content="hi", _sessions_base=sessions_base)
        assert "Error" in r1 and "app" in r1.lower()

        r2 = await app_notify(action="write", app="x", _sessions_base=sessions_base)
        assert "Error" in r2 and "content" in r2.lower()


@pytest.mark.asyncio
async def test_app_notify_clear(tmp_path):
    """clear removes the notification file."""
    from nutshell.tool_engine.providers.app_notify import app_notify

    sessions_base = tmp_path / "sessions"
    sid = "test-session"
    apps_dir = sessions_base / sid / "core" / "apps"
    apps_dir.mkdir(parents=True)
    (apps_dir / "alert.md").write_text("Fire alarm!")

    with patch.dict("os.environ", {"NUTSHELL_SESSION_ID": sid}):
        result = await app_notify(action="clear", app="alert", _sessions_base=sessions_base)
        assert "Cleared" in result
        assert not (apps_dir / "alert.md").exists()


@pytest.mark.asyncio
async def test_app_notify_clear_nonexistent(tmp_path):
    """clear on missing file returns informative message."""
    from nutshell.tool_engine.providers.app_notify import app_notify

    sessions_base = tmp_path / "sessions"
    sid = "test-session"
    (sessions_base / sid / "core").mkdir(parents=True)

    with patch.dict("os.environ", {"NUTSHELL_SESSION_ID": sid}):
        result = await app_notify(action="clear", app="ghost", _sessions_base=sessions_base)
        assert "No notification found" in result


@pytest.mark.asyncio
async def test_app_notify_unknown_action(tmp_path):
    """Unknown action returns error."""
    from nutshell.tool_engine.providers.app_notify import app_notify

    sessions_base = tmp_path / "sessions"
    sid = "test-session"
    (sessions_base / sid / "core").mkdir(parents=True)

    with patch.dict("os.environ", {"NUTSHELL_SESSION_ID": sid}):
        result = await app_notify(action="delete", _sessions_base=sessions_base)
        assert "Error" in result and "unknown action" in result.lower()


@pytest.mark.asyncio
async def test_app_notify_sanitizes_app_name(tmp_path):
    """Dangerous characters in app name are stripped."""
    from nutshell.tool_engine.providers.app_notify import app_notify

    sessions_base = tmp_path / "sessions"
    sid = "test-session"
    (sessions_base / sid / "core").mkdir(parents=True)

    with patch.dict("os.environ", {"NUTSHELL_SESSION_ID": sid}):
        result = await app_notify(
            action="write", app="../../../etc/passwd", content="hacked",
            _sessions_base=sessions_base,
        )
        assert "Written" in result
        # Should create a safe filename, not traverse directories
        assert "etcpasswd.md" in result


@pytest.mark.asyncio
async def test_app_notify_registered_as_builtin():
    """app_notify is registered in the global tool registry."""
    from nutshell.tool_engine.registry import get_builtin
    impl = get_builtin("app_notify")
    assert impl is not None
    assert callable(impl)


def test_app_notify_json_schema_exists():
    """entity/agent/tools/app_notify.json exists and is valid."""
    schema_path = Path(__file__).parent.parent / "entity" / "agent" / "tools" / "app_notify.json"
    assert schema_path.exists(), "app_notify.json not found"
    data = json.loads(schema_path.read_text())
    assert data["name"] == "app_notify"
    assert "action" in data["input_schema"]["properties"]
    assert "app" in data["input_schema"]["properties"]
    assert "content" in data["input_schema"]["properties"]
    assert data["input_schema"]["required"] == ["action"]


def test_app_notify_in_agent_yaml():
    """app_notify.json is listed in entity/agent/agent.yaml tools."""
    yaml_path = Path(__file__).parent.parent / "entity" / "agent" / "agent.yaml"
    text = yaml_path.read_text()
    assert "tools/app_notify.json" in text


# ── System prompt injection ───────────────────────────────────────


def test_agent_app_notifications_empty():
    """No app notifications → no App Notifications block in prompt."""
    agent = Agent(system_prompt="Hello")
    agent.app_notifications = []
    prompt = "\n".join(p for p in agent._build_system_parts() if p)
    assert "App Notifications" not in prompt


def test_agent_app_notifications_rendered():
    """App notifications appear as ## App Notifications in system prompt."""
    agent = Agent(system_prompt="Hello")
    agent.app_notifications = [
        ("weather", "Sunny 25°C"),
        ("alerts", "Server CPU > 90%"),
    ]
    prompt = "\n".join(p for p in agent._build_system_parts() if p)
    assert "## App Notifications" in prompt
    assert "### weather" in prompt
    assert "Sunny 25°C" in prompt
    assert "### alerts" in prompt
    assert "Server CPU > 90%" in prompt


def test_agent_app_notifications_order():
    """App notifications appear after memory and before skills."""
    agent = Agent(system_prompt="base")
    agent.memory = "some memory"
    agent.app_notifications = [("test-app", "notification content")]
    # Add a minimal skill to check ordering
    from nutshell.core.skill import Skill
    agent.skills = [Skill(name="dummy", description="d", body="skill body")]
    prompt = "\n".join(p for p in agent._build_system_parts() if p)

    mem_pos = prompt.index("Session Memory")
    notif_pos = prompt.index("App Notifications")
    skill_pos = prompt.index("dummy")
    assert mem_pos < notif_pos < skill_pos


# ── Session integration ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_loads_app_notifications(tmp_path):
    """Session reads core/apps/*.md and injects them into agent."""
    session = _make_session(tmp_path)

    # Create app notification files
    apps_dir = session.core_dir / "apps"
    apps_dir.mkdir()
    (apps_dir / "chat.md").write_text("3 unread messages")
    (apps_dir / "monitor.md").write_text("All systems green")

    # Trigger capability reload (happens inside chat)
    await session.chat("hello")

    agent = session._agent
    assert len(agent.app_notifications) == 2
    names = [n for n, _ in agent.app_notifications]
    assert "chat" in names
    assert "monitor" in names


@pytest.mark.asyncio
async def test_session_skips_empty_app_files(tmp_path):
    """Empty .md files in core/apps/ are not loaded."""
    session = _make_session(tmp_path)

    apps_dir = session.core_dir / "apps"
    apps_dir.mkdir()
    (apps_dir / "empty.md").write_text("")
    (apps_dir / "real.md").write_text("content here")

    await session.chat("hello")

    agent = session._agent
    assert len(agent.app_notifications) == 1
    assert agent.app_notifications[0][0] == "real"


@pytest.mark.asyncio
async def test_session_no_apps_dir(tmp_path):
    """No core/apps/ directory → empty app_notifications."""
    session = _make_session(tmp_path)
    await session.chat("hello")
    assert session._agent.app_notifications == []
