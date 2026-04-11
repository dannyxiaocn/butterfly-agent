# nutshell_dev — Design

Project-development entity for this repository. Extends `agent` with nutshell-specific memory, a project skill, and a task-picking heartbeat.

## Purpose

Turn the generic runtime into a project-aware maintainer. This is the repo's built-in self-hosting developer persona.

## Inheritance

- **From agent**: system.md, session.md, tools, creator-mode skill
- **Own**: heartbeat.md (autonomous track.md task selection), memory, playground
- **Linked**: model, provider, fallback_model, fallback_provider (from agent)
- **Appended**: skills (adds nutshell skill)
