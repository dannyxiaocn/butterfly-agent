# Agent Skills — Implementation

## creator-mode

`SKILL.md` teaches an agent how to create or modify tools and skills inside its own session (`core/tools/` and `core/skills/`) and hot-reload them with `reload_capabilities`.

## Adding a New Skill

1. Create a directory here with `SKILL.md`
2. List it in `entity/agent/agent.yaml` under `skills:`
3. All inheriting entities will receive it
