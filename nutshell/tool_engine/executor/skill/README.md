# nutshell/tool_engine/executor/skill

This directory implements the built-in `skill` tool.

## What It Is

- `skill_tool.py`: `SkillExecutor`, variable substitution for skill arguments, and `create_skill_tool()`

## How To Use It

The runtime injects the tool automatically when a session exposes `skill.json`. A model calls it with a skill name and optional raw argument string.

## How It Fits

This is the progressive-disclosure mechanism for `SKILL.md`. `skill_engine/renderer.py` advertises available skills, and this executor loads the full body only when requested.
