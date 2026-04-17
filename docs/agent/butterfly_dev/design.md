# butterfly_dev — Design

Project-development agent for this repository. Extends `agent` with butterfly-specific memory, a project skill, and autonomous task execution.

## Purpose

Turn the generic runtime into a project-aware maintainer. This is the repo's built-in self-hosting developer persona.

## Inheritance

- **From agent**: system.md, env.md, tools, creator-mode skill
- **Own**: task.md (autonomous task execution), memory, playground
- **Linked**: model, provider, fallback_model, fallback_provider (from agent)
- **Appended**: skills (adds butterfly skill)
