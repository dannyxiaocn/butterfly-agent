"""Tests for task-based session tick (replaces old persistent mode tests).

Covers:
  - session_config.py: duty field in config
  - session.py tick(): task card fires and writes triggered_by='task:<name>'
  - session.py tick(): returns None when no due cards
  - session_init: duty config propagates to task cards
"""

import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path

from butterfly.core.agent import Agent
from butterfly.core.provider import Provider
from butterfly.core.types import AgentResult, TokenUsage, ToolCall
from butterfly.session_engine.session_config import read_config, write_config
from butterfly.session_engine.task_cards import TaskCard, load_card, save_card, ensure_card


# ── Helpers ────────────────────────────────────────────────────────


class MockProvider(Provider):
    """A mock provider that returns pre-configured responses."""

    def __init__(self, responses):
        self._responses = iter(responses)

    async def complete(self, messages, tools, system_prompt, model, *,
                       on_text_chunk=None, cache_system_prefix="",
                       cache_last_human_turn=False, thinking: bool = False, thinking_budget: int = 8000, thinking_effort: str = "high", on_thinking_start=None, on_thinking_end=None):
        r = next(self._responses)
        return (r[0], r[1], r[2] if len(r) > 2 else TokenUsage())


# ── session_config — duty field ────────────────────────────────���─


def test_config_has_duty_field():
    """DEFAULT_CONFIG includes duty=None."""
    from butterfly.session_engine.session_config import DEFAULT_CONFIG
    assert "duty" in DEFAULT_CONFIG
    assert DEFAULT_CONFIG["duty"] is None


def test_write_read_config_duty(tmp_path):
    """write_config persists duty; read returns it."""
    session_dir = tmp_path / "session"
    core = session_dir / "core"
    core.mkdir(parents=True)
    write_config(session_dir, duty={"interval": 3600, "description": "Check mail"})
    cfg = read_config(session_dir)
    assert cfg["duty"]["interval"] == 3600
    assert cfg["duty"]["description"] == "Check mail"


# ── session.py tick() — task-based mode ──────────────────────────


@pytest.mark.asyncio
async def test_tick_returns_none_when_no_due_cards(tmp_path):
    """tick() returns None when no task cards are due."""
    from butterfly.session_engine.session import Session

    provider = MockProvider([("should not be called", [])])
    agent = Agent(provider=provider)

    session = Session(
        agent,
        session_id="test-no-tasks",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )
    result = await session.tick()
    assert result is None


@pytest.mark.asyncio
async def test_tick_fires_due_task_card(tmp_path):
    """tick() picks up a due task card and runs it."""
    from butterfly.session_engine.session import Session

    provider = MockProvider([("Task done.", [])])
    agent = Agent(provider=provider)

    session = Session(
        agent,
        session_id="test-task-fire",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )

    past = (datetime.now() - timedelta(hours=2)).isoformat()
    save_card(session.tasks_dir, TaskCard(
        name="check", description="Check state", interval=600,
        start_at=past,
    ))

    result = await session.tick()
    assert result is not None
    assert result.content == "Task done."

    # Card should be marked pending (recurring) after finish
    card = load_card(session.tasks_dir, "check")
    assert card.status == "pending"
    assert card.last_finished_at is not None


@pytest.mark.asyncio
async def test_tick_writes_triggered_by_task(tmp_path):
    """tick() writes triggered_by='task:<name>' in context."""
    from butterfly.session_engine.session import Session

    provider = MockProvider([("ok", [])])
    agent = Agent(provider=provider)

    session = Session(
        agent,
        session_id="test-trigger",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )

    past = (datetime.now() - timedelta(hours=2)).isoformat()
    save_card(session.tasks_dir, TaskCard(name="duty", description="Do stuff", interval=600, start_at=past))

    result = await session.tick()
    assert result is not None

    turns = [
        json.loads(line)
        for line in session._context_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert turns[-1]["type"] == "turn"
    assert turns[-1]["triggered_by"] == "task:duty"


@pytest.mark.asyncio
async def test_tick_with_explicit_card(tmp_path):
    """tick(card) runs the specified card regardless of is_due()."""
    from butterfly.session_engine.session import Session

    provider = MockProvider([("done with task", [])])
    agent = Agent(provider=provider)

    session = Session(
        agent,
        session_id="test-explicit",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )

    card = TaskCard(name="manual", description="- do something real", interval=None)
    save_card(session.tasks_dir, card)

    result = await session.tick(card)
    assert result is not None

    turns = [
        json.loads(line)
        for line in session._context_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert turns[-1]["type"] == "turn"
    assert turns[-1]["triggered_by"] == "task:manual"


# ── session_init — duty propagation ──────────────────────────────


def test_session_init_creates_duty_card_from_config(tmp_path):
    """init_session creates a duty task card when config.yaml has duty field."""
    from butterfly.session_engine.session_init import init_session
    from unittest.mock import patch

    agent_base = tmp_path / "agenthub"
    agent_dir = agent_base / "test_ent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.yaml").write_text(
        "name: test_ent\n"
        "model: claude-sonnet-4-6\n"
        "provider: anthropic\n"
        "duty:\n"
        "  interval: 3600\n"
        '  description: "Check mail"\n',
        encoding="utf-8",
    )
    (agent_dir / "tools.md").write_text("bash\n", encoding="utf-8")
    (agent_dir / "prompts").mkdir()
    (agent_dir / "prompts" / "system.md").write_text("sys", encoding="utf-8")
    (agent_dir / "prompts" / "task.md").write_text("task", encoding="utf-8")
    (agent_dir / "prompts" / "env.md").write_text("env", encoding="utf-8")

    sessions_base = tmp_path / "sessions"
    system_base = tmp_path / "_sessions"

    def fake_venv(session_dir):
        venv = session_dir / ".venv"
        venv.mkdir(parents=True, exist_ok=True)
        return venv

    with patch("butterfly.session_engine.session_init._create_session_venv", side_effect=fake_venv), \
         patch("butterfly.session_engine.agent_state._create_meta_venv", side_effect=fake_venv):
        init_session(
            "s1",
            "test_ent",
            sessions_base=sessions_base,
            system_sessions_base=system_base,
            agent_base=agent_base,
        )

    duty = load_card(sessions_base / "s1" / "core" / "tasks", "duty")
    assert duty is not None
    assert duty.description == "Check mail"
    assert duty.interval == 3600
    # v2.0.6 regression pin: duty cards must default to end_at=None so
    # long-running agents don't silently auto-expire after 7 days.
    assert duty.end_at is None


def test_session_init_no_duty_keeps_empty_tasks(tmp_path):
    """init_session without duty in config keeps tasks dir empty (no task card)."""
    from butterfly.session_engine.session_init import init_session
    from unittest.mock import patch

    agent_base = tmp_path / "agenthub"
    agent_dir = agent_base / "plain"
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.yaml").write_text(
        "name: plain\nmodel: claude-sonnet-4-6\nprovider: anthropic\n",
        encoding="utf-8",
    )
    (agent_dir / "prompts").mkdir()
    (agent_dir / "prompts" / "system.md").write_text("sys", encoding="utf-8")

    sessions_base = tmp_path / "sessions"
    system_base = tmp_path / "_sessions"

    def fake_venv(session_dir):
        venv = session_dir / ".venv"
        venv.mkdir(parents=True, exist_ok=True)
        return venv

    with patch("butterfly.session_engine.session_init._create_session_venv", side_effect=fake_venv), \
         patch("butterfly.session_engine.agent_state._create_meta_venv", side_effect=fake_venv):
        init_session(
            "s2",
            "plain",
            sessions_base=sessions_base,
            system_sessions_base=system_base,
            agent_base=agent_base,
        )

    tasks_dir = sessions_base / "s2" / "core" / "tasks"
    from butterfly.session_engine.task_cards import load_all_cards
    # Should have no duty card (meta task may exist from start_meta_agent)
    duty = load_card(tasks_dir, "duty")
    assert duty is None
