# Skill Engine — Design

The skill engine loads `SKILL.md` files from disk and renders the skill catalog for the agent's system prompt.

## Design Principles

- **Progressive disclosure**: the prompt shows a catalog; the agent loads full skill body only when needed via the `skill` tool
- **File-backed**: skills are directories with `SKILL.md` and optional support files
- **Compact prompts**: skill catalog in system prompt is a summary; full content loaded on demand
