"""Example 04: Main agent dynamically creates a temporary sub-agent.

The main agent (orchestrator) spins up a short-lived "tmp" sub-agent
for each task. The sub-agent uses release_policy="auto" so its history
is wiped after every call — it truly acts as a stateless throwaway worker.
"""
import asyncio
from nutshell import Agent, AnthropicProvider


async def main():
    # --- Temporary sub-agent (stateless, history cleared after each call) ---
    tmp_agent = Agent(
        system_prompt=(
            "You are a concise analyst. "
            "Answer in 1-2 sentences, no fluff."
        ),
        model="claude-haiku-4-5-20251001",
        provider=AnthropicProvider(),
        release_policy="auto",   # <-- history wiped after every call: truly "tmp"
    )

    # Wrap the tmp agent as a tool so the orchestrator can call it
    analyse_tool = tmp_agent.as_tool(
        name="analyse",
        description=(
            "Delegate a single analytical question to a temporary sub-agent. "
            "The sub-agent has no memory of previous calls."
        ),
    )

    # --- Main orchestrator agent ---
    orchestrator = Agent(
        system_prompt=(
            "You are an orchestrator. "
            "For every user request, break it into sub-questions and call "
            "'analyse' for each one, then synthesize a final answer."
        ),
        tools=[analyse_tool],
        model="claude-haiku-4-5-20251001",
        provider=AnthropicProvider(),
    )

    print("=== Main agent + tmp sub-agent demo ===\n")
    result = await orchestrator.run(
        "Compare the speed of light vs the speed of sound, "
        "and tell me which is faster and by roughly how much."
    )

    print("Orchestrator final answer:")
    print(result.content)
    print(f"\nTotal tool calls made: {len(result.tool_calls)}")
    for tc in result.tool_calls:
        print(f"  [{tc.name}] input: {tc.input.get('input', '')[:80]}")


if __name__ == "__main__":
    asyncio.run(main())
