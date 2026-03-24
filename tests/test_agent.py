import pytest
from unittest.mock import AsyncMock
from nutshell.core.agent import Agent
from nutshell.core.provider import Provider
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
async def test_inline_skill_injected_into_system_prompt():
    """Inline skills (no location) have their body injected directly."""
    skill = Skill(
        name="math",
        description="Math expert",
        body="You are a math genius.",
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
    assert "Math expert" in captured["system_prompt"]
    assert "You are a math genius." in captured["system_prompt"]


@pytest.mark.asyncio
async def test_file_skill_uses_catalog_in_system_prompt(tmp_path):
    """File-backed skills (with location) appear as a catalog entry, not inline."""
    skill_dir = tmp_path / "math"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("---\nname: math\ndescription: Math expert\n---\n\nYou are a math genius.\n")

    from nutshell.skill_engine.loader import SkillLoader
    skill = SkillLoader().load(skill_dir)

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
    sp = captured["system_prompt"]
    # Catalog metadata present
    assert "math" in sp
    assert "Math expert" in sp
    assert str(skill_md) in sp
    # Body NOT injected inline
    assert "You are a math genius." not in sp


@pytest.mark.asyncio
async def test_as_tool():
    provider = MockProvider([("Summary of topic X.", [])])
    sub_agent = Agent(provider=provider, release_policy="auto")
    sub_tool = sub_agent.as_tool("summarize", "Summarize a topic")

    result = await sub_tool.execute(input="topic X")
    assert result == "Summary of topic X."


@pytest.mark.asyncio
async def test_as_tool_clear_history_resets_persistent_subagent_each_call():
    provider = MockProvider([
        ("First", []),
        ("Second", []),
    ])
    sub_agent = Agent(provider=provider, release_policy="persistent")
    sub_tool = sub_agent.as_tool("summarize", "Summarize a topic", clear_history=True)

    await sub_tool.execute(input="topic X")
    assert len(sub_agent._history) == 2
    assert sub_agent._history[0].content == "topic X"

    await sub_tool.execute(input="topic Y")
    assert len(sub_agent._history) == 2
    assert sub_agent._history[0].content == "topic Y"


@pytest.mark.asyncio
async def test_as_tool_preserves_history_by_default_for_persistent_subagent():
    provider = MockProvider([
        ("First", []),
        ("Second", []),
    ])
    sub_agent = Agent(provider=provider, release_policy="persistent")
    sub_tool = sub_agent.as_tool("summarize", "Summarize a topic")

    await sub_tool.execute(input="topic X")
    await sub_tool.execute(input="topic Y")

    assert len(sub_agent._history) == 4
    assert sub_agent._history[0].content == "topic X"
    assert sub_agent._history[2].content == "topic Y"


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
