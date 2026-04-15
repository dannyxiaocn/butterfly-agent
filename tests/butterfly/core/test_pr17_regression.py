from __future__ import annotations

import pytest
from butterfly.core.agent import Agent
from butterfly.core.provider import Provider
from butterfly.core.types import TokenUsage, ToolCall
from butterfly.core.tool import tool


# ── 1. _get_fallback_provider silent failure ─────────────────────────────────

class _FailingProvider(Provider):
    async def complete(
        self,
        messages,
        tools,
        system_prompt,
        model,
        *,
        on_text_chunk=None,
        cache_system_prefix="",
        cache_last_human_turn=False,
        thinking=False,
        thinking_budget=8000,
        thinking_effort="high",
    ):
        raise RuntimeError("primary failure")


def test_get_fallback_provider_reuses_primary_when_only_fallback_model() -> None:
    """If only fallback_model is provided, _get_fallback_provider silently returns None."""
    agent = Agent(fallback_model="gpt-fallback", fallback_provider="")
    assert agent._get_fallback_provider() is agent.provider


@pytest.mark.asyncio
async def test_fallback_model_alone_does_not_catch_primary_failure() -> None:
    """A primary failure is not recovered when only fallback_model is configured."""
    agent = Agent(provider=_FailingProvider(), fallback_model="gpt-fallback")
    with pytest.raises(RuntimeError, match="primary failure"):
        await agent.run("hi")


# ── 2. consume_extra_blocks + text + tool calls simultaneously ───────────────

class _MixedProvider(Provider):
    """Returns reasoning blocks alongside text content and a tool call."""

    def __init__(self) -> None:
        self._turn = 0
        self._reasoning = [
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "OPAQUE"}
        ]

    def consume_extra_blocks(self) -> list[dict]:
        if self._turn == 1:
            return self._reasoning
        return []

    async def complete(
        self,
        messages,
        tools,
        system_prompt,
        model,
        *,
        on_text_chunk=None,
        cache_system_prefix="",
        cache_last_human_turn=False,
        thinking=False,
        thinking_budget=8000,
        thinking_effort="high",
    ):
        self._turn += 1
        if self._turn == 1:
            return (
                "hello",
                [ToolCall(id="tc-1", name="noop", input={})],
                TokenUsage(output_tokens=1),
            )
        return ("done", [], TokenUsage(output_tokens=1))


@pytest.mark.asyncio
async def test_consume_extra_blocks_with_text_and_tool_calls() -> None:
    """Extra blocks, text, and tool_use must all appear in the assistant message."""

    @tool
    def noop() -> str:
        return "ok"

    agent = Agent(tools=[noop], provider=_MixedProvider(), max_iterations=1)
    result = await agent.run("go")

    # messages: user, assistant (with blocks), tool_result
    assert len(result.messages) == 3
    asst_msg = result.messages[1]
    assert asst_msg.role == "assistant"
    assert isinstance(asst_msg.content, list)

    block_types = [b.get("type") for b in asst_msg.content if isinstance(b, dict)]
    assert block_types == ["reasoning", "text", "tool_use"]

    reasoning = asst_msg.content[0]
    assert reasoning["id"] == "rs_1"
    assert reasoning["encrypted_content"] == "OPAQUE"

    text = asst_msg.content[1]
    assert text["text"] == "hello"

    tool_use = asst_msg.content[2]
    assert tool_use["name"] == "noop"


# ── 3. TokenUsage.__add__ with reasoning_tokens ──────────────────────────────


def test_token_usage_add_preserves_reasoning_tokens() -> None:
    a = TokenUsage(
        input_tokens=10,
        output_tokens=5,
        reasoning_tokens=3,
        cache_read_tokens=1,
        cache_write_tokens=2,
    )
    b = TokenUsage(
        input_tokens=2,
        output_tokens=4,
        reasoning_tokens=7,
        cache_read_tokens=10,
        cache_write_tokens=20,
    )
    c = a + b
    assert c.input_tokens == 12
    assert c.output_tokens == 9
    assert c.reasoning_tokens == 10
    assert c.cache_read_tokens == 11
    assert c.cache_write_tokens == 22


# ── 5. Dead getattr fallback for missing consume_extra_blocks ────────────────

class _LegacyProvider:
    """A provider-shaped object that does NOT inherit from Provider."""

    async def complete(
        self,
        messages,
        tools,
        system_prompt,
        model,
        *,
        on_text_chunk=None,
        cache_system_prefix="",
        cache_last_human_turn=False,
        thinking=False,
        thinking_budget=8000,
        thinking_effort="high",
    ):
        return ("legacy", [], TokenUsage())


@pytest.mark.asyncio
async def test_base_provider_consume_extra_blocks_default_is_empty_list():
    """Post-fix (OBS-9): the Provider ABC defines consume_extra_blocks so the
    agent's getattr-fallback path is dead code and was removed. Verify the ABC
    default returns []."""
    from butterfly.core.provider import Provider

    class _Stub(Provider):
        async def complete(self, *args, **kwargs):
            return ("", [], None)

    stub = _Stub()
    assert stub.consume_extra_blocks() == []


