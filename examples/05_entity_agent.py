"""Example 05: Load an agent from entity/ folder using file loaders.

Demonstrates Layer 3 (entity/) + Layer 1 loaders working together.
The agent's system prompt, tools, and skills all come from external files
under entity/core_agent/ — no hardcoded strings in Python.
"""
import asyncio
from pathlib import Path

from nutshell import Agent, AnthropicProvider
from nutshell.loaders import PromptLoader, SkillLoader, ToolLoader

REPO_ROOT = Path(__file__).parent.parent
AGENT_DIR = REPO_ROOT / "entity" / "core_agent"


def build_agent() -> Agent:
    # Load system prompt from .md file
    system_prompt = PromptLoader().load(AGENT_DIR / "prompts" / "system.md")

    # Load skills from .md files with YAML frontmatter
    skills = SkillLoader().load_dir(AGENT_DIR / "skills")

    # Load tool schemas from .json; wire Python implementations via impl_registry
    tool_loader = ToolLoader(impl_registry={
        "echo": lambda text: text,
    })
    tools = tool_loader.load_dir(AGENT_DIR / "tools")

    return Agent(
        system_prompt=system_prompt,
        tools=tools,
        skills=skills,
        model="claude-haiku-4-5-20251001",
        provider=AnthropicProvider(),
    )


async def main():
    agent = build_agent()

    print("=== Entity Agent Demo ===")
    print(f"Prompt : {AGENT_DIR / 'prompts' / 'system.md'}")
    print(f"Tools  : {[t.name for t in agent.tools]}")
    print(f"Skills : {[s.name for s in agent.skills]}")
    print()

    result = await agent.run('Please echo the text "Hello from entity layer!"')
    print("Response:", result.content)
    if result.tool_calls:
        print(f"Tool calls: {[tc.name for tc in result.tool_calls]}")


if __name__ == "__main__":
    asyncio.run(main())
