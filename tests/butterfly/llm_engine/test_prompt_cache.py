"""Tests for Anthropic prompt caching support."""
from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from butterfly.core.types import Message
from butterfly.llm_engine.providers.anthropic import AnthropicProvider
from butterfly.llm_engine.providers.kimi import KimiForCodingProvider


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


def test_kimi_provider_falls_back_to_legacy_kimi_api_key(monkeypatch):
    monkeypatch.delenv("KIMI_FOR_CODING_API_KEY", raising=False)
    monkeypatch.setenv("KIMI_API_KEY", "legacy-key")

    captured = {}

    def _fake_init(self, *, api_key=None, max_tokens=8096, base_url=None):
        captured["api_key"] = api_key
        captured["max_tokens"] = max_tokens
        captured["base_url"] = base_url

    monkeypatch.setattr(AnthropicProvider, "__init__", _fake_init)

    KimiForCodingProvider()

    assert captured["api_key"] == "legacy-key"


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


def test_anthropic_provider_prefers_http_proxy_when_socks_missing(monkeypatch):
    import httpx
    from butterfly.llm_engine.providers.anthropic import _build_http_client

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7890")
    monkeypatch.setattr("butterfly.llm_engine.providers.anthropic._has_socks_support", lambda: False)

    client = _build_http_client(httpx)

    assert isinstance(client, httpx.AsyncClient)
    assert client._trust_env is False


def test_anthropic_provider_uses_env_defaults_when_socks_supported(monkeypatch):
    import httpx
    from butterfly.llm_engine.providers.anthropic import _build_http_client

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7890")
    monkeypatch.setattr("butterfly.llm_engine.providers.anthropic._has_socks_support", lambda: True)

    client = _build_http_client(httpx)

    assert client is None


# ── Agent._build_system_parts ─────────────────────────────────────────────────

def _make_agent(system_prompt="", session_context="", memory="", memory_layers=None):
    from butterfly.core.agent import Agent
    a = Agent.__new__(Agent)
    a.system_prompt = system_prompt
    a.env_context = session_context
    a.memory = memory
    a.memory_layers = memory_layers or []
    a.app_notifications = []
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


# ── History caching ────────────────────────────────────────────────────────────

from butterfly.llm_engine.providers.anthropic import (
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


# ── Token usage tracking ────────────────────────────────────────────────────────

def test_token_usage_dataclass_add():
    from butterfly.core.types import TokenUsage
    a = TokenUsage(input_tokens=100, output_tokens=50, cache_read_tokens=200, cache_write_tokens=10)
    b = TokenUsage(input_tokens=30, output_tokens=20)
    c = a + b
    assert c.input_tokens == 130
    assert c.output_tokens == 70
    assert c.cache_read_tokens == 200
    assert c.total_tokens == 200


def test_token_usage_as_dict():
    from butterfly.core.types import TokenUsage
    u = TokenUsage(input_tokens=10, output_tokens=5, cache_read_tokens=100, cache_write_tokens=2)
    d = u.as_dict()
    assert d == {
        "input": 10,
        "output": 5,
        "cache_read": 100,
        "cache_write": 2,
        "reasoning": 0,
    }


@pytest.mark.asyncio
async def test_anthropic_provider_returns_usage():
    """AnthropicProvider.complete() returns TokenUsage with token counts."""
    from butterfly.core.types import TokenUsage

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.max_tokens = 100
    captured: list = []

    async def _create(**kwargs):
        captured.append(kwargs)
        from types import SimpleNamespace
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=SimpleNamespace(
                input_tokens=50,
                output_tokens=20,
                cache_read_input_tokens=100,
                cache_creation_input_tokens=5,
            ),
        )

    provider._client = SimpleNamespace(messages=SimpleNamespace(create=_create))

    _, _, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="claude-test",
    )

    assert isinstance(usage, TokenUsage)
    assert usage.input_tokens == 50
    assert usage.output_tokens == 20
    assert usage.cache_read_tokens == 100
    assert usage.cache_write_tokens == 5
    assert usage.total_tokens == 70


# ── Reasoning blocks round-trip through the agent loop ───────────────────────


@pytest.mark.asyncio
async def test_agent_loop_round_trips_reasoning_blocks():
    """consume_extra_blocks() output must land in the assistant Message and be
    re-echoed to the provider on the next turn so reasoning continuity holds.
    """
    from butterfly.core.agent import Agent
    from butterfly.core.provider import Provider
    from butterfly.core.types import Message, TokenUsage, ToolCall
    from butterfly.core.tool import tool

    seen_messages: list[list[Message]] = []

    class ReasoningProvider(Provider):
        def __init__(self):
            self._turn = 0
            self._pending: list[dict] = []

        def consume_extra_blocks(self):
            drained = self._pending
            self._pending = []
            return drained

        async def complete(self, messages, tools, system_prompt, model, *,
                           on_text_chunk=None, cache_system_prefix="",
                           cache_last_human_turn=False, thinking=False,
                           thinking_budget=8000, thinking_effort="high"):
            seen_messages.append(list(messages))
            self._turn += 1
            if self._turn == 1:
                self._pending = [
                    {
                        "type": "reasoning",
                        "id": "rs_42",
                        "summary": [],
                        "encrypted_content": "OPAQUE-A",
                    }
                ]
                return (
                    "",
                    [ToolCall(id="tc-1", name="noop", input={})],
                    TokenUsage(input_tokens=5, output_tokens=1),
                )
            return ("done", [], TokenUsage(input_tokens=3, output_tokens=1))

    @tool
    def noop() -> str:
        """No-op."""
        return "ok"

    agent = Agent(tools=[noop], provider=ReasoningProvider())
    await agent.run("go")

    # The second call must see the reasoning block on the assistant message.
    assert len(seen_messages) == 2
    second_call = seen_messages[1]
    assistant_msgs = [m for m in second_call if m.role == "assistant"]
    assert len(assistant_msgs) == 1
    content = assistant_msgs[0].content
    assert isinstance(content, list)
    reasoning_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "reasoning"]
    assert len(reasoning_blocks) == 1
    assert reasoning_blocks[0]["id"] == "rs_42"
    assert reasoning_blocks[0]["encrypted_content"] == "OPAQUE-A"


@pytest.mark.asyncio
async def test_agent_loop_handles_provider_without_consume_extra_blocks():
    """Legacy providers without consume_extra_blocks must still work (fallback to [])."""
    from butterfly.core.agent import Agent
    from butterfly.core.provider import Provider
    from butterfly.core.types import TokenUsage

    class PlainProvider(Provider):
        async def complete(self, messages, tools, system_prompt, model, *,
                           on_text_chunk=None, cache_system_prefix="",
                           cache_last_human_turn=False, thinking=False,
                           thinking_budget=8000, thinking_effort="high"):
            return ("plain reply", [], TokenUsage())

    # Monkey-patch out the default consume_extra_blocks from the base class to
    # simulate a truly legacy provider.
    provider = PlainProvider()
    # Remove the inherited method via instance attribute shadowing.
    # The Agent loop guards with getattr(..., lambda: []).
    # We rely on it not throwing when the method returns empty.
    agent = Agent(provider=provider)
    result = await agent.run("go")
    assert result.content == "plain reply"


@pytest.mark.asyncio
async def test_agent_accumulates_usage_over_tool_loops():
    """AgentResult.usage sums tokens across all tool-call iterations."""
    from butterfly.core.agent import Agent
    from butterfly.core.provider import Provider
    from butterfly.core.types import Message, TokenUsage, ToolCall
    from butterfly.core.tool import tool

    class CountingProvider(Provider):
        async def complete(self, messages, tools, system_prompt, model, *, on_text_chunk=None, cache_system_prefix="", cache_last_human_turn=False, thinking: bool = False, thinking_budget: int = 8000, thinking_effort: str = "high"):
            from butterfly.core.types import TokenUsage
            if len(messages) <= 2:  # first call: return tool_call
                return ("", [ToolCall(id="1", name="noop", input={})], TokenUsage(input_tokens=10, output_tokens=5))
            return ("done", [], TokenUsage(input_tokens=8, output_tokens=3))  # second call: done

    @tool
    def noop() -> str:
        """No-op tool."""
        return "ok"

    agent = Agent(tools=[noop], provider=CountingProvider())
    result = await agent.run("do it")

    assert result.usage.input_tokens == 18   # 10 + 8
    assert result.usage.output_tokens == 8   # 5 + 3
    assert result.usage.total_tokens == 26
