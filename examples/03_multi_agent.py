"""Example 03: Multi-agent patterns.

Pattern A: Agent-as-Tool (orchestrator calls sub-agent as a tool)
Pattern B: Message passing (manual pipeline)
"""
import asyncio
from nutshell import Agent, AnthropicProvider


def make_provider():
    return AnthropicProvider()


async def pattern_a_agent_as_tool():
    """Orchestrator delegates writing to a sub-agent via as_tool()."""
    print("--- Pattern A: Agent-as-Tool ---")

    writer = Agent(
        system_prompt="You are a creative writer. Write vivid, short paragraphs.",
        model="claude-haiku-4-5-20251001",
        provider=make_provider(),
        release_policy="auto",  # history cleared after each tool call
    )

    orchestrator = Agent(
        system_prompt=(
            "You coordinate agents. When asked to write something, "
            "always delegate to the 'write_paragraph' tool."
        ),
        tools=[writer.as_tool("write_paragraph", "Write a short paragraph on a topic.")],
        model="claude-haiku-4-5-20251001",
        provider=make_provider(),
    )

    result = await orchestrator.run("Write a paragraph about the ocean.")
    print(result.content)


async def pattern_b_message_passing():
    """Researcher feeds output to a summarizer via manual pipeline."""
    print("\n--- Pattern B: Message Passing ---")

    researcher = Agent(
        system_prompt="You are a researcher. Provide 3 key facts about any topic.",
        model="claude-haiku-4-5-20251001",
        provider=make_provider(),
    )

    summarizer = Agent(
        system_prompt="You summarize content into a single sentence.",
        model="claude-haiku-4-5-20251001",
        provider=make_provider(),
    )

    research_result = await researcher.run("Tell me about black holes.")
    summary_result = await summarizer.run(research_result.content)
    print(summary_result.content)


async def main():
    await pattern_a_agent_as_tool()
    await pattern_b_message_passing()


if __name__ == "__main__":
    asyncio.run(main())
