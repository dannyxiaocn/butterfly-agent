"""Tests for persistent agent feature.

Covers:
  - params.py: persistent and default_task fields
  - session.py tick(): persistent mode fires with default_task when tasks empty
  - session.py tick(): non-persistent mode skips when tasks empty (existing behaviour)
  - session_factory: entity params (persistent, default_task) propagate to params.json
  - entity/persistent_agent/ loads successfully
"""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock

from nutshell.core.agent import Agent
from nutshell.core.types import AgentResult, TokenUsage, ToolCall
from nutshell.runtime.params import DEFAULT_PARAMS, read_session_params, write_session_params


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


# ── params.py — new fields ────────────────────────────────────────


def test_default_params_has_persistent():
    """DEFAULT_PARAMS includes persistent=False."""
    assert "persistent" in DEFAULT_PARAMS
    assert DEFAULT_PARAMS["persistent"] is False


def test_default_params_has_default_task():
    """DEFAULT_PARAMS includes default_task=None."""
    assert "default_task" in DEFAULT_PARAMS
    assert DEFAULT_PARAMS["default_task"] is None


def test_read_params_persistent_defaults(tmp_path):
    """read_session_params returns persistent=False when params.json has no persistent key."""
    session_dir = tmp_path / "session"
    core = session_dir / "core"
    core.mkdir(parents=True)
    (core / "params.json").write_text("{}", encoding="utf-8")
    params = read_session_params(session_dir)
    assert params["persistent"] is False
    assert params["default_task"] is None


def test_write_read_persistent_params(tmp_path):
    """write_session_params persists persistent + default_task; read returns them."""
    session_dir = tmp_path / "session"
    write_session_params(session_dir, persistent=True, default_task="Check mail")
    params = read_session_params(session_dir)
    assert params["persistent"] is True
    assert params["default_task"] == "Check mail"


# ── session.py tick() — persistent mode ───────────────────────────


@pytest.mark.asyncio
async def test_tick_skips_when_not_persistent_and_empty_tasks(tmp_path):
    """tick() returns None when tasks empty and persistent=False (default)."""
    from nutshell.runtime.session import Session

    provider = MockProvider([("should not be called", [])])
    agent = Agent(provider=provider)

    session = Session(
        agent,
        session_id="test-non-persistent",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )
    # tasks.md is empty by default
    assert session.tasks_path.read_text().strip() == ""

    result = await session.tick()
    assert result is None


@pytest.mark.asyncio
async def test_tick_fires_with_default_task_when_persistent(tmp_path):
    """tick() triggers LLM with default_task when persistent=True and tasks empty."""
    from nutshell.runtime.session import Session

    provider = MockProvider([("All clear, nothing to do.", [])])
    agent = Agent(provider=provider)

    session = Session(
        agent,
        session_id="test-persistent",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )
    # tasks.md is empty
    assert session.tasks_path.read_text().strip() == ""

    # Enable persistent mode with a custom default_task
    write_session_params(
        session.session_dir,
        persistent=True,
        default_task="Check incoming messages.",
    )

    result = await session.tick()
    assert result is not None
    assert result.content == "All clear, nothing to do."


@pytest.mark.asyncio
async def test_tick_persistent_uses_builtin_fallback_when_no_default_task(tmp_path):
    """tick() uses built-in fallback prompt when persistent=True but default_task is None."""
    from nutshell.runtime.session import Session

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

    write_session_params(session.session_dir, persistent=True, default_task=None)

    # Monkeypatch agent.run to capture the prompt
    agent.run = lambda msg, **kw: capturing_run(agent, msg, **kw)

    result = await session.tick()
    assert result is not None
    assert len(captured_prompts) == 1
    # Should contain the fallback prompt text
    assert "Check for incoming messages" in captured_prompts[0]


@pytest.mark.asyncio
async def test_tick_persistent_triggered_by_heartbeat_default(tmp_path):
    """tick() writes triggered_by='heartbeat_default' into persisted turn context."""
    from nutshell.runtime.session import Session

    provider = MockProvider([("ok", [])])
    agent = Agent(provider=provider)

    session = Session(
        agent,
        session_id="test-persistent-trigger",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )

    write_session_params(session.session_dir, persistent=True, default_task="Check state")

    result = await session.tick()
    assert result is not None

    turns = [
        json.loads(line)
        for line in session._context_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert turns[-1]["type"] == "turn"
    assert turns[-1]["triggered_by"] == "heartbeat_default"


@pytest.mark.asyncio
async def test_tick_with_real_tasks_ignores_persistent(tmp_path):
    """When tasks exist, tick() uses them normally regardless of persistent flag."""
    from nutshell.runtime.session import Session

    provider = MockProvider([("done with task", [])])
    agent = Agent(provider=provider)

    session = Session(
        agent,
        session_id="test-persistent-tasks",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )

    session.tasks_path.write_text("- do something real", encoding="utf-8")
    write_session_params(session.session_dir, persistent=True, default_task="Check state")

    result = await session.tick()
    assert result is not None

    turns = [
        json.loads(line)
        for line in session._context_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert turns[-1]["type"] == "turn"
    assert turns[-1]["triggered_by"] == "heartbeat"


# ── session_factory — entity params propagation ───────────────────


def test_session_factory_propagates_entity_params(tmp_path):
    """init_session reads params from agent.yaml and writes them to params.json."""
    from nutshell.runtime.session_factory import init_session

    # Create a minimal entity with params
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
    assert params["persistent"] is True
    assert params["default_task"] == "Hello world"
    assert params["heartbeat_interval"] == 43200


def test_session_factory_no_params_key_defaults(tmp_path):
    """init_session without params key in agent.yaml keeps defaults."""
    from nutshell.runtime.session_factory import init_session

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
    assert params["persistent"] is False
    assert params["default_task"] is None


# ── entity/persistent_agent/ loads ────────────────────────────────


def test_persistent_agent_entity_loads():
    """entity/persistent_agent/ can be loaded by AgentLoader."""
    from nutshell.runtime.agent_loader import AgentLoader

    entity_dir = Path(__file__).parent.parent / "entity" / "persistent_agent"
    if not entity_dir.exists():
        pytest.skip("entity/persistent_agent/ not found")

    agent = AgentLoader().load(entity_dir)
    assert agent is not None
    assert "persistent" in agent.system_prompt.lower()


def test_persistent_agent_yaml_has_params():
    """agent.yaml declares persistent=true and heartbeat_interval=43200."""
    import yaml

    yaml_path = Path(__file__).parent.parent / "entity" / "persistent_agent" / "agent.yaml"
    if not yaml_path.exists():
        pytest.skip("entity/persistent_agent/agent.yaml not found")

    manifest = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    params = manifest.get("params", {})
    assert params.get("persistent") is True
    assert params.get("heartbeat_interval") == 43200
    assert params.get("default_task") is not None
