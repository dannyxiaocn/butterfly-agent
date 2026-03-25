"""Tests for Anthropic prompt caching support."""
from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from nutshell.core.types import Message
from nutshell.llm_engine.providers.anthropic import AnthropicProvider
from nutshell.llm_engine.providers.kimi import KimiForCodingProvider


# ── AnthropicProvider cache_system_prefix ─────────────────────────────────────

def _fake_client(captured: list) -> SimpleNamespace:
    """Return a fake client that captures kwargs passed to messages.create."""
    async def _create(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")])

    return SimpleNamespace(messages=SimpleNamespace(create=_create))


@pytest.mark.asyncio
async def test_anthropic_uses_block_list_when_cache_prefix_given():
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.max_tokens = 100
    captured: list = []
    provider._client = _fake_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="dynamic part",
        model="claude-test",
        cache_system_prefix="static part",
    )

    assert len(captured) == 1
    system = captured[0]["system"]
    assert isinstance(system, list), "system should be a block list"
    assert system[0]["type"] == "text"
    assert system[0]["text"] == "static part"
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[1]["type"] == "text"
    assert system[1]["text"] == "dynamic part"


@pytest.mark.asyncio
async def test_anthropic_uses_string_when_no_cache_prefix():
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.max_tokens = 100
    captured: list = []
    provider._client = _fake_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="full prompt",
        model="claude-test",
    )

    assert captured[0]["system"] == "full prompt"


@pytest.mark.asyncio
async def test_anthropic_omits_empty_dynamic_block():
    """When system_prompt is empty but cache_system_prefix is set, only one block emitted."""
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.max_tokens = 100
    captured: list = []
    provider._client = _fake_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="",
        model="claude-test",
        cache_system_prefix="static only",
    )

    system = captured[0]["system"]
    assert isinstance(system, list)
    # Only one block — the static prefix; no empty second block
    assert len(system) == 1
    assert system[0]["text"] == "static only"


# ── KimiProvider ignores cache_control ────────────────────────────────────────

def test_kimi_provider_does_not_support_cache_control():
    assert KimiForCodingProvider._supports_cache_control is False


@pytest.mark.asyncio
async def test_kimi_provider_falls_back_to_string_with_prefix():
    """KimiProvider should concatenate prefix+prompt instead of using block list."""
    provider = KimiForCodingProvider.__new__(KimiForCodingProvider)
    provider.max_tokens = 100
    captured: list = []
    provider._client = _fake_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="dynamic",
        model="kimi-test",
        cache_system_prefix="static",
    )

    system = captured[0]["system"]
    assert isinstance(system, str), "KimiProvider must NOT send a block list"
    assert "static" in system
    assert "dynamic" in system


# ── Agent._build_system_parts ─────────────────────────────────────────────────

def _make_agent(system_prompt="", session_context="", memory="", memory_layers=None):
    from nutshell.core.agent import Agent
    a = Agent.__new__(Agent)
    a.system_prompt = system_prompt
    a.session_context = session_context
    a.memory = memory
    a.memory_layers = memory_layers or []
    a.skills = []
    return a


def test_agent_build_system_parts_static_and_dynamic_split():
    agent = _make_agent(
        system_prompt="You are an agent.",
        session_context="Session info here.",
        memory="Remember this.",
    )
    prefix, suffix = agent._build_system_parts()

    assert "You are an agent." in prefix
    assert "Session info here." in prefix
    assert "Remember this." in suffix
    # Memory should NOT be in prefix
    assert "Remember this." not in prefix


def test_agent_build_system_parts_no_memory_empty_suffix():
    agent = _make_agent(
        system_prompt="Hello",
        session_context="ctx",
    )
    prefix, suffix = agent._build_system_parts()
    assert "Hello" in prefix
    assert suffix == ""


def test_agent_build_system_parts_memory_layers_in_suffix():
    agent = _make_agent(
        system_prompt="sys",
        memory_layers=[("project", "project content")],
    )
    prefix, suffix = agent._build_system_parts()
    assert "project content" in suffix
    assert "project content" not in prefix


def test_agent_build_system_prompt_still_returns_full_string():
    """_build_system_prompt() must stay backward-compatible."""
    agent = _make_agent(
        system_prompt="sys",
        session_context="ctx",
        memory="mem",
    )
    full = agent._build_system_prompt()
    assert "sys" in full
    assert "ctx" in full
    assert "mem" in full
