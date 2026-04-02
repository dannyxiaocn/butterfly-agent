# Entity Catalog

This directory contains the built-in agent entities shipped with nutshell.

## Active entities

| Entity | Purpose | Status |
|---|---|---|
| `agent` | General-purpose default assistant with core tools and multi-agent skills | canonical base entity |
| `cli_os` | Shell-heavy exploration agent for immersive CLI / VM-style tasks | active specialist |
| `game_player` | Game-solving specialist for puzzles, strategy, and challenge workflows | active specialist |
| `kimi_agent` | Kimi-based specialist inheriting the default agent stack | active provider variant |
| `dev_maintainer` | Persistent code-quality maintainer for tests, simplification, and cleanup guidance | active internal maintainer |
| `nutshell_dev` | Nutshell development agent with project-specific skills and task heartbeat | active internal developer |
| `nutshell_dev_codex` | Codex-flavoured nutshell development agent with dedicated memory templates | active internal developer |
| `persistent_agent` | Always-on background agent with long heartbeat and default maintenance task | active runtime utility |
| `tool_manager` | Persistent audit analyst for tool/skill usage trends, efficiency, and improvement suggestions | active runtime utility |
| `tool_craftsman` | Persistent craftsman focused on improving tool and skill definitions, ergonomics, and guardrails | active internal maintainer |
| `receptionist` | Front-desk coordinator that delegates work to worker agents | active coordination specialist |
| `yisebi` | Opinionated social-commentary specialist for social-media style tasks | active specialist |

## Curation notes

- No built-in entity is currently archived or deprecated.
- Internal development entities (`nutshell_dev`, `nutshell_dev_codex`) are part of the maintained built-in surface because the repo uses them operationally.
- If an entity is later retired, move it out of this active list and mark it explicitly as archived rather than leaving its purpose ambiguous.
