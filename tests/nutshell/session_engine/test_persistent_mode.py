"""Tests for session_type feature (ephemeral / default / persistent).

Covers:
  - params.py: session_type and default_task fields
  - session.py tick(): persistent mode fires with default_task when tasks empty
  - session.py tick(): default mode skips when tasks empty (existing behaviour)
  - session.py _resolve_session_type(): backward compat for persistent bool
  - session_factory: entity params (session_type, default_task) propagate to params.json
"""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock

from nutshell.core.agent import Agent
from nutshell.core.types import AgentResult, TokenUsage, ToolCall
from nutshell.session_engine.session_params import DEFAULT_PARAMS, read_session_params, write_session_params
from nutshell.session_engine.task_cards import TaskCard, load_card, save_card


# ── Helpers ────────────────────────────────────────────────────────


class MockProvider:
    """A mock provider that returns pre-configured responses."""

    def __init__(self, responses):
        self._responses = iter(responses)

    async def complete(self, messages, tools, system_prompt, model, *,
                       on_text_chunk=None, cache_system_prefix="",
                       cache_last_human_turn=False, thinking: bool = False, thinking_budget: int = 8000, thinking_effort: str = "high"):
        r = next(self._responses)
        return (r[0], r[1], r[2] if len(r) > 2 else TokenUsage())


# ── params.py — session_type field ──────────────────────────────────


def test_default_params_has_session_type():
    """DEFAULT_PARAMS includes session_type='default'."""
    assert "session_type" in DEFAULT_PARAMS
    assert DEFAULT_PARAMS["session_type"] == "default"


def test_default_params_has_default_task():
    """DEFAULT_PARAMS includes default_task=None."""
    assert "default_task" in DEFAULT_PARAMS
    assert DEFAULT_PARAMS["default_task"] is None


def test_read_params_session_type_defaults(tmp_path):
    """read_session_params returns session_type='default' when params.json is empty."""
    session_dir = tmp_path / "session"
    core = session_dir / "core"
    core.mkdir(parents=True)
    (core / "params.json").write_text("{}", encoding="utf-8")
    params = read_session_params(session_dir)
    assert params["session_type"] == "default"
    assert params["default_task"] is None


def test_write_read_session_type_params(tmp_path):
    """write_session_params persists session_type + default_task; read returns them."""
    session_dir = tmp_path / "session"
    write_session_params(session_dir, session_type="persistent", default_task="Check mail")
    params = read_session_params(session_dir)
    assert params["session_type"] == "persistent"
    assert params["default_task"] == "Check mail"


# ── _resolve_session_type backward compat ────────────────────────


def test_resolve_session_type_backward_compat():
    """_resolve_session_type converts old persistent=True to 'persistent'."""
    from nutshell.session_engine.session import Session
    assert Session._resolve_session_type({"persistent": True}) == "persistent"
    assert Session._resolve_session_type({"persistent": False}) == "default"
    assert Session._resolve_session_type({}) == "default"
    assert Session._resolve_session_type({"session_type": "ephemeral"}) == "ephemeral"
    assert Session._resolve_session_type({"session_type": "persistent"}) == "persistent"


# ── session.py tick() — persistent mode ───────────────────────────


@pytest.mark.asyncio
async def test_tick_skips_when_default_and_empty_tasks(tmp_path):
    """tick() returns None when tasks empty and session_type='default'."""
    from nutshell.session_engine.session import Session

    provider = MockProvider([("should not be called", [])])
    agent = Agent(provider=provider)

    session = Session(
        agent,
        session_id="test-non-persistent",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )
    assert not session.tasks_path.exists()
    assert list(session.tasks_dir.glob("*.md")) == []

    result = await session.tick(load_card(session.tasks_dir, "real"))
    assert result is None


@pytest.mark.asyncio
async def test_tick_migrates_default_task_into_heartbeat_card_when_persistent(tmp_path):
    """Legacy default_task is migrated into the heartbeat card and runs from there."""
    from nutshell.session_engine.session import Session

    provider = MockProvider([("All clear, nothing to do.", [])])
    agent = Agent(provider=provider)

    session = Session(
        agent,
        session_id="test-persistent",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )
    assert not session.tasks_path.exists()

    write_session_params(
        session.session_dir,
        session_type="persistent",
        default_task="Check incoming messages.",
    )

    result = await session.tick(load_card(session.tasks_dir, "real"))
    assert result is not None
    assert result.content == "All clear, nothing to do."
    heartbeat = load_card(session.tasks_dir, "heartbeat")
    assert heartbeat is not None
    assert heartbeat.content == "Check incoming messages."


@pytest.mark.asyncio
async def test_tick_persistent_uses_builtin_fallback_when_no_default_task(tmp_path):
    """tick() uses built-in fallback prompt when session_type='persistent' but default_task is None."""
    from nutshell.session_engine.session import Session

    captured_prompts: list[str] = []
    original_run = Agent.run

    async def capturing_run(self, message, **kwargs):
        captured_prompts.append(message)
        return AgentResult(content="resting", iterations=1, usage=TokenUsage())

    provider = MockProvider([])  # won't be used — we monkeypatch run()
    agent = Agent(provider=provider)

    session = Session(
        agent,
        session_id="test-persistent-fallback",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )

    write_session_params(session.session_dir, session_type="persistent", default_task=None)

    # Monkeypatch agent.run to capture the prompt
    agent.run = lambda msg, **kw: capturing_run(agent, msg, **kw)

    result = await session.tick()
    assert result is not None
    assert len(captured_prompts) == 1
    # Should contain the fallback prompt text
    assert "Check for incoming messages" in captured_prompts[0]


@pytest.mark.asyncio
async def test_tick_persistent_triggered_by_heartbeat(tmp_path):
    """tick() writes triggered_by='heartbeat' for the heartbeat task card."""
    from nutshell.session_engine.session import Session

    provider = MockProvider([("ok", [])])
    agent = Agent(provider=provider)

    session = Session(
        agent,
        session_id="test-persistent-trigger",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )

    write_session_params(session.session_dir, session_type="persistent", default_task="Check state")

    result = await session.tick()
    assert result is not None

    turns = [
        json.loads(line)
        for line in session._context_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert turns[-1]["type"] == "turn"
    assert turns[-1]["triggered_by"] == "heartbeat"


@pytest.mark.asyncio
async def test_tick_with_real_tasks_ignores_session_type(tmp_path):
    """When tasks exist, tick() uses them normally regardless of session_type."""
    from nutshell.session_engine.session import Session

    provider = MockProvider([("done with task", [])])
    agent = Agent(provider=provider)

    session = Session(
        agent,
        session_id="test-persistent-tasks",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )

    save_card(session.tasks_dir, TaskCard(name="real", content="- do something real", interval=None))
    write_session_params(session.session_dir, session_type="persistent", default_task="Check state")

    result = await session.tick(load_card(session.tasks_dir, "real"))
    assert result is not None

    turns = [
        json.loads(line)
        for line in session._context_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert turns[-1]["type"] == "turn"
    assert turns[-1]["triggered_by"] == "task:real"


# ── session_factory — entity params propagation ───────────────────


def test_session_factory_propagates_entity_params(tmp_path):
    """init_session reads params from agent.yaml and writes them to params.json."""
    from nutshell.session_engine.session_init import init_session

    # Create a minimal entity with params (using legacy persistent: true)
    entity_base = tmp_path / "entity"
    entity_dir = entity_base / "test_ent"
    entity_dir.mkdir(parents=True)
    (entity_dir / "agent.yaml").write_text(
        "name: test_ent\n"
        "model: claude-sonnet-4-6\n"
        "provider: anthropic\n"
        "tools: []\n"
        "skills: []\n"
        "params:\n"
        "  persistent: true\n"
        '  default_task: "Hello world"\n'
        "  heartbeat_interval: 43200\n",
        encoding="utf-8",
    )

    sessions_base = tmp_path / "sessions"
    system_base = tmp_path / "_sessions"

    init_session(
        "s1",
        "test_ent",
        sessions_base=sessions_base,
        system_sessions_base=system_base,
        entity_base=entity_base,
    )

    params = read_session_params(sessions_base / "s1")
    # Legacy persistent: true should be converted to session_type: persistent
    assert params["session_type"] == "persistent"
    assert params["default_task"] is None
    assert params["heartbeat_interval"] == 43200
    heartbeat = load_card(sessions_base / "s1" / "core" / "tasks", "heartbeat")
    assert heartbeat is not None
    assert heartbeat.content == "Hello world"
    assert heartbeat.interval == 43200


def test_session_factory_no_params_key_defaults(tmp_path):
    """init_session without params key in agent.yaml keeps defaults."""
    from nutshell.session_engine.session_init import init_session

    entity_base = tmp_path / "entity"
    entity_dir = entity_base / "plain"
    entity_dir.mkdir(parents=True)
    (entity_dir / "agent.yaml").write_text(
        "name: plain\nmodel: claude-sonnet-4-6\nprovider: anthropic\ntools: []\nskills: []\n",
        encoding="utf-8",
    )

    sessions_base = tmp_path / "sessions"
    system_base = tmp_path / "_sessions"

    init_session(
        "s2",
        "plain",
        sessions_base=sessions_base,
        system_sessions_base=system_base,
        entity_base=entity_base,
    )

    params = read_session_params(sessions_base / "s2")
    assert params["session_type"] == "default"
    assert params["default_task"] is None
