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


# ── History caching ────────────────────────────────────────────────────────────

from nutshell.llm_engine.providers.anthropic import (
    _find_cache_breakpoint,
    _to_api_messages,
)


def test_find_cache_breakpoint_returns_last_non_final_user():
    msgs = [
        Message(role="user", content="first"),
        Message(role="assistant", content="response"),
        Message(role="user", content="second"),   # ← should be the breakpoint
        Message(role="assistant", content="final"),  # current response
    ]
    # We add the final assistant message after; breakpoint should be index 2
    # But _find_cache_breakpoint looks at messages[-2] and finds last user/assistant
    # The 'messages' passed include the new user msg at end; the one before it is index 2
    only_history = msgs[:3]   # simulate: [*history, new_user_msg]
    bp = _find_cache_breakpoint(only_history)
    assert bp == 1  # last non-final (assistant at index 1)


def test_find_cache_breakpoint_single_message_returns_none():
    msgs = [Message(role="user", content="only")]
    assert _find_cache_breakpoint(msgs) is None


def test_find_cache_breakpoint_empty_returns_none():
    assert _find_cache_breakpoint([]) is None


def test_to_api_messages_adds_cache_control_at_breakpoint():
    msgs = [
        Message(role="user", content="hello"),
        Message(role="assistant", content="world"),
        Message(role="user", content="new input"),
    ]
    result = _to_api_messages(msgs, cache_breakpoint_index=1)
    # index 1 (assistant "world") should have cache_control
    assert result[0]["content"] == "hello"
    content_1 = result[1]["content"]
    assert isinstance(content_1, list)
    assert content_1[-1]["cache_control"] == {"type": "ephemeral"}
    assert result[2]["content"] == "new input"


def test_to_api_messages_no_breakpoint_unchanged():
    msgs = [
        Message(role="user", content="hello"),
        Message(role="assistant", content="world"),
    ]
    result = _to_api_messages(msgs)
    assert result[0]["content"] == "hello"
    assert result[1]["content"] == "world"


def test_to_api_messages_cache_on_list_content():
    """cache_control should be appended to last block of a list content."""
    msgs = [
        Message(role="assistant", content=[
            {"type": "text", "text": "part1"},
            {"type": "text", "text": "part2"},
        ]),
        Message(role="user", content="new"),
    ]
    result = _to_api_messages(msgs, cache_breakpoint_index=0)
    content = result[0]["content"]
    assert isinstance(content, list)
    assert content[-1]["cache_control"] == {"type": "ephemeral"}
    assert content[-1]["text"] == "part2"


@pytest.mark.asyncio
async def test_anthropic_passes_cache_breakpoint_when_history_present():
    """When cache_last_human_turn=True and history exists, breakpoint is set."""
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.max_tokens = 100
    captured: list = []
    provider._client = _fake_client(captured)

    msgs = [
        Message(role="user", content="previous"),
        Message(role="assistant", content="prev-reply"),
        Message(role="user", content="current"),
    ]

    await provider.complete(
        messages=msgs,
        tools=[],
        system_prompt="sys",
        model="claude-test",
        cache_last_human_turn=True,
    )

    # The second message (assistant, index 1) should have cache_control
    api_msgs = captured[0]["messages"]
    # Find the assistant message content
    asst_content = api_msgs[1]["content"]
    assert isinstance(asst_content, list)
    assert asst_content[-1].get("cache_control") == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_kimi_provider_ignores_cache_last_human_turn():
    """KimiProvider must NOT set cache_control on messages."""
    provider = KimiForCodingProvider.__new__(KimiForCodingProvider)
    provider.max_tokens = 100
    captured: list = []
    provider._client = _fake_client(captured)

    msgs = [
        Message(role="user", content="previous"),
        Message(role="assistant", content="prev-reply"),
        Message(role="user", content="current"),
    ]

    await provider.complete(
        messages=msgs,
        tools=[],
        system_prompt="sys",
        model="kimi-test",
        cache_last_human_turn=True,
    )

    api_msgs = captured[0]["messages"]
    # Assistant message should be plain string, no cache_control blocks
    asst_content = api_msgs[1]["content"]
    assert isinstance(asst_content, str), "Kimi must not add cache_control blocks to messages"
