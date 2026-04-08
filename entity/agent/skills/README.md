# `entity/agent/skills`

This is the base skill catalog shipped with the default entity.

## What Is Active

- `creator-mode/`: instructions for creating and hot-reloading session tools and skills.

Some sibling directories exist but are currently empty and are not active skills until they contain a `SKILL.md`.

## How To Use This Part

Add a new skill directory here and list it in `entity/agent/agent.yaml` if it should ship with the base entity.

## How It Contributes To The Whole System

These skills are inherited by higher-level entities and are the reusable workflow layer above prompts and tools.

