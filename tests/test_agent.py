import pytest
from unittest.mock import AsyncMock
from nutshell.core.agent import Agent
from nutshell.abstract.provider import Provider
from nutshell.core.skill import Skill
from nutshell.core.tool import tool
from nutshell.core.types import ToolCall


class MockProvider(Provider):
    """A mock provider for testing without real API calls."""

    def __init__(self, responses):
        # responses: list of (content, tool_calls) tuples
        self._responses = iter(responses)

    async def complete(self, messages, tools, system_prompt, model, *, on_text_chunk=None):
        return next(self._responses)


@pytest.mark.asyncio
async def test_basic_run():
    provider = MockProvider([("Hello, world!", [])])
    agent = Agent(system_prompt="You are helpful.", provider=provider)
    result = await agent.run("Hi")
    assert result.content == "Hello, world!"
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_history_preserved():
    provider = MockProvider([
        ("Paris.", []),
        ("French.", []),
    ])
    agent = Agent(provider=provider)
    await agent.run("Capital of France?")
    await agent.run("Language?")
    # History should have 4 messages: user, assistant, user, assistant
    assert len(agent._history) == 4


@pytest.mark.asyncio
async def test_history_cleared_on_close():
    provider = MockProvider([("ok", [])])
    agent = Agent(provider=provider, release_policy="manual")
    await agent.run("hello")
    assert len(agent._history) > 0
    agent.close()
    assert agent._history == []


@pytest.mark.asyncio
async def test_auto_release_policy():
    provider = MockProvider([("ok", [])])
    agent = Agent(provider=provider, release_policy="auto")
    await agent.run("hello")
    assert agent._history == []


@pytest.mark.asyncio
async def test_tool_call_loop():
    calc_call = ToolCall(id="1", name="add", input={"a": 1, "b": 2})

    provider = MockProvider([
        ("", [calc_call]),        # first call: returns tool_call
        ("The answer is 3.", []),  # second call: final answer
    ])

    @tool(description="Add numbers")
    def add(a: int, b: int) -> int:
        return a + b

    agent = Agent(provider=provider, tools=[add])
    result = await agent.run("What is 1 + 2?")

    assert result.content == "The answer is 3."
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "add"


@pytest.mark.asyncio
async def test_skill_injected_into_system_prompt():
    skill = Skill(
        name="math",
        description="Math expert",
        prompt_injection="You are a math genius.",
    )

    captured = {}

    class CapturingProvider(Provider):
        async def complete(self, messages, tools, system_prompt, model, *, on_text_chunk=None):
            captured["system_prompt"] = system_prompt
            return ("ok", [])

    agent = Agent(
        system_prompt="Base prompt.",
        skills=[skill],
        provider=CapturingProvider(),
    )
    await agent.run("hello")
    assert "You are a math genius." in captured["system_prompt"]


@pytest.mark.asyncio
async def test_as_tool():
    provider = MockProvider([("Summary of topic X.", [])])
    sub_agent = Agent(provider=provider, release_policy="auto")
    sub_tool = sub_agent.as_tool("summarize", "Summarize a topic")

    result = await sub_tool.execute(input="topic X")
    assert result == "Summary of topic X."


@pytest.mark.asyncio
async def test_unknown_tool_returns_error():
    bad_call = ToolCall(id="x", name="nonexistent", input={})

    provider = MockProvider([
        ("", [bad_call]),
        ("I could not find the tool.", []),
    ])
    agent = Agent(provider=provider)
    result = await agent.run("call nonexistent tool")
    assert result.content == "I could not find the tool."
