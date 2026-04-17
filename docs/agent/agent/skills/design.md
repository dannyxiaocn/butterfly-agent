# Agent Skills — Design

Base skill catalog shipped with the default agent. Skills are the reusable workflow layer above prompts and tools, inherited by higher-level agents.

## Active Skills

- `creator-mode/`: teaches agents to create/modify tools and skills, then hot-reload with `reload_capabilities`

## Reserved Placeholders

- `messaging/`, `model-selection/`, `multi-agent/`, `qjbq/`: inactive directories reserved for future use
