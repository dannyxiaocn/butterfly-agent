"""Example 01: Basic agent with a custom system prompt."""
import asyncio
from nutshell import Agent, AnthropicProvider

async def main():
    agent = Agent(
        system_prompt="You are a concise assistant. Always reply in one sentence.",
        model="claude-haiku-4-5-20251001",
        provider=AnthropicProvider(),  # reads ANTHROPIC_API_KEY from env
    )

    result = await agent.run("What is the capital of France?")
    print(result.content)

    # Second turn — history is preserved (release_policy="persistent" by default)
    result2 = await agent.run("What language do they speak there?")
    print(result2.content)

if __name__ == "__main__":
    asyncio.run(main())
