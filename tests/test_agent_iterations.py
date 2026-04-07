"""Tests for AgentResult iteration counting and agent tool loops."""

import pytest

from nutshell.core.agent import Agent
from nutshell.core.tool import tool
from nutshell.core.types import AgentResult, TokenUsage, ToolCall


class MockProvider:
    """A mock provider that returns pre-configured responses."""

    def __init__(self, responses):
        self._responses = iter(responses)

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
        thinking: bool = False,
        thinking_budget: int = 8000,
        thinking_effort: str = "high",
    ):
        response = next(self._responses)
        return (response[0], response[1], response[2] if len(response) > 2 else TokenUsage())


def test_agent_result_iterations_default():
    """iterations defaults to 0."""
    result = AgentResult(content="hello")
    assert result.iterations == 0


def test_agent_result_iterations_explicit():
    """iterations can be set explicitly."""
    result = AgentResult(content="hello", iterations=5)
    assert result.iterations == 5


@pytest.mark.asyncio
async def test_single_iteration_no_tools():
    """A simple completion with no tool calls = 1 iteration."""
    provider = MockProvider([("Hello!", [])])
    agent = Agent(provider=provider)
    result = await agent.run("Hi")
    assert result.iterations == 1


@pytest.mark.asyncio
async def test_multiple_iterations_with_tool_loop():
    """A tool-call loop of N rounds = N iterations."""
    calc_call = ToolCall(id="1", name="add", input={"a": 1, "b": 2})

    provider = MockProvider([
        ("", [calc_call]),
        ("", [calc_call]),
        ("The answer is 3.", []),
    ])

    @tool(description="Add numbers")
    def add(a: int, b: int) -> int:
        return a + b

    agent = Agent(provider=provider, tools=[add])
    result = await agent.run("What is 1 + 2?")
    assert result.iterations == 3
    assert result.content == "The answer is 3."


@pytest.mark.asyncio
async def test_single_tool_call_two_iterations():
    """One tool call = 2 iterations (call + final answer)."""
    calc_call = ToolCall(id="1", name="add", input={"a": 5, "b": 3})

    provider = MockProvider([
        ("", [calc_call]),
        ("8", []),
    ])

    @tool(description="Add numbers")
    def add(a: int, b: int) -> int:
        return a + b

    agent = Agent(provider=provider, tools=[add])
    result = await agent.run("5 + 3?")
    assert result.iterations == 2


@pytest.mark.asyncio
async def test_iterations_with_token_usage():
    """iterations is set alongside usage."""
    usage = TokenUsage(input_tokens=100, output_tokens=50)
    provider = MockProvider([("ok", [], usage)])
    agent = Agent(provider=provider)
    result = await agent.run("test")
    assert result.iterations == 1
    assert result.usage.input_tokens == 100
