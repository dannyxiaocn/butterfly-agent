"""Example 02: Agent with custom tools using the @tool decorator."""
import asyncio
from nutshell import Agent, AnthropicProvider, tool


@tool(description="Add two integers and return the sum.")
def add(a: int, b: int) -> int:
    return a + b


@tool(description="Search the web (stub). Returns fake results.")
async def web_search(query: str) -> str:
    # Replace with a real search API in production
    return f"Top result for '{query}': 42 is the answer to everything."


async def main():
    agent = Agent(
        system_prompt="You are a helpful assistant with access to tools.",
        tools=[add, web_search],
        model="claude-haiku-4-5-20251001",
        provider=AnthropicProvider(),
    )

    result = await agent.run("What is 17 + 25?")
    print(result.content)
    print(f"Tool calls made: {[tc.name for tc in result.tool_calls]}")

if __name__ == "__main__":
    asyncio.run(main())
