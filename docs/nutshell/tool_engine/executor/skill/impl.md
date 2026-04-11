# Skill Executor — Implementation

## Files

- `skill_tool.py`: `SkillExecutor`, variable substitution for skill arguments, `create_skill_tool()`

## How It Works

1. `skill_engine/renderer.py` advertises available skills in the system prompt
2. Agent calls the `skill` tool with a skill name and optional arguments
3. `SkillExecutor` loads the full `SKILL.md` body, applies `$ARGUMENTS` substitution
4. Returns the skill content for the agent to use

The runtime injects this tool automatically when a session exposes `skill.json`.
