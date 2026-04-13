---
name: dev-nutshell
description: >
  Development context for the Nutshell codebase. Load this skill for tasks that
  change Nutshell itself: runtime work, provider changes, session lifecycle,
  CLI or web changes, tool/skill engine work, entity updates, documentation,
  tests, or repo-level maintenance.
---

# Nutshell Developer Guide

Read the code and tests before trusting any documentation. Keep changes local to the task scope. Update docs and tests together with behavior changes.

## Repo Layout

```text
nutshell/           runtime implementation
├── core/           Agent, Tool, Skill, Provider ABCs, types, BaseLoader
├── llm_engine/     provider registry + adapters (anthropic, openai, kimi, codex)
├── tool_engine/    tool loading, executors, registry
├── skill_engine/   SKILL.md loading + system prompt rendering
├── session_engine/ entity config, session init, meta-session state, task cards
└── runtime/        server, watcher, IPC, bridge, env, git coordination
toolhub/            built-in tool implementations (tool.json + executor.py)
skillhub/           built-in skill definitions (SKILL.md)
ui/
├── cli/            `nutshell` CLI entry point
└── web/            FastAPI + SSE + Vite frontend
entity/             agent templates (config.yaml + prompts/ + tools.md + skills.md)
tests/              mirrors source layout
docs/               documentation and task boards
```

## Key Design Principles

### Filesystem-as-Everything
- Agents read/write session directories; IPC via `context.jsonl` + `events.jsonl`
- `entity/` is read-only template; all mutable state in `sessions/`
- `sessions/<entity>_meta/` holds entity-level mutable state

### Hub Pattern (toolhub + skillhub)
- All built-in tools live in `toolhub/<name>/` with `tool.json` + `executor.py`
- All built-in skills live in `skillhub/<name>/SKILL.md`
- Entity and session `tools.md` / `skills.md` only list **enabled** names (one per line)
- Agent-created tools (`core/tools/`) and skills (`core/skills/`) are session-local extensions

### Progressive Disclosure (Skills)
- File-backed skills render as `<available_skills>` catalog in system prompt
- Model loads full skill body on demand via the `skill` tool
- Inline skills (no file location) inject body directly

### Dependency Flow
```
UI → runtime → session_engine → core
```
- `session_engine` never imports `runtime` (except `git_coordinator`)
- `core` should stay low-dependency, but currently depends on `llm_engine` and `skill_engine` in a few places

## Package Boundaries

| Package | Owns | Does NOT own |
|---------|------|-------------|
| `core/` | Agent loop, Tool/Skill/Provider dataclasses, types | Loading, lifecycle |
| `llm_engine/` | Provider implementations, message conversion | Tool execution |
| `tool_engine/` | ToolLoader, executor dispatch, shell/bash tools | Agent loop |
| `skill_engine/` | SkillLoader, skills.md parsing, prompt rendering | Tool execution |
| `session_engine/` | Session lifecycle, entity config, meta-session, task cards | Central dispatch |
| `runtime/` | Server, watcher, IPC, bridge | Entity config |

## Session Model

```
entity/<name>/           read-only template
  ├── config.yaml        model, provider, thinking, prompts
  ├── prompts/           system.md, task.md, env.md
  ├── tools.md           enabled toolhub tools (one name per line)
  └── skills.md          enabled skillhub skills (one name per line)

sessions/<id>/           agent-visible runtime
  └── core/
      ├── config.yaml    runtime config (from meta session)
      ├── system.md      system prompt
      ├── task.md        task/heartbeat prompt
      ├── env.md         session environment context
      ├── memory.md      persistent memory (injected every activation)
      ├── memory/        named memory layers (*.md)
      ├── tools.md       enabled toolhub tools
      ├── skills.md      enabled skillhub skills
      ├── tools/         agent-created tools (.json + .sh pairs)
      ├── skills/        agent-created skills (SKILL.md dirs)
      └── tasks/         task cards (*.md with YAML frontmatter)

_sessions/<id>/          system-only twin (agent never sees)
  ├── manifest.json      entity name, created_at
  ├── status.json        dynamic runtime state
  ├── context.jsonl      conversation records
  └── events.jsonl       live runtime events for UI streaming
```

## How to Add Things

### Adding a built-in tool

1. Create `toolhub/<name>/tool.json` (Anthropic tool schema format)
2. Create `toolhub/<name>/executor.py` with an executor class
3. Register special context injection in `nutshell/tool_engine/loader.py` `_create_executor()` if needed
4. Add the tool name to relevant entity `tools.md` files
5. Update docs and tests

### Adding a provider

1. Create or update `nutshell/llm_engine/providers/<name>.py`
2. Register in `nutshell/llm_engine/registry.py`
3. Align with `nutshell/core/provider.py` contract
4. Verify: message conversion, tool calls, streaming, token usage

### Adding a built-in skill

1. Create `skillhub/<name>/SKILL.md` with frontmatter + body
2. Add the skill name to relevant entity `skills.md` files
3. Update docs

### Adding an entity

1. Create `entity/<name>/` with `config.yaml`, `prompts/`, `tools.md`, `skills.md`
2. Run `nutshell meta <name> --init` to initialize meta session

## System Prompt Assembly

Order (top to bottom):
1. `system.md` — static system prompt (cached)
2. `env.md` — session environment context (cached)
3. `memory.md` — persistent memory
4. `memory/*.md` — named memory layers (truncated at 60 lines each)
5. App notifications (`core/apps/*.md`)
6. Skills catalog (`<available_skills>` block)

Static prefix (`system.md` + `env.md`) uses Anthropic `cache_control: ephemeral`.

## Testing

Run the smallest scope first:
```bash
pytest tests/nutshell/skill_engine/ -q
pytest tests/nutshell/session_engine/ -q
pytest tests/ -q
```

Test layout mirrors source: `tests/nutshell/{core,llm_engine,tool_engine,...}/`

## Practical Heuristics

- If a README and the code disagree, trust the code and fix the README
- If a directory is an operational subsystem, it should have a short README
- If a file path is part of a contract, mention the path exactly as the code uses it
- Use `nutshell meta agent --sync entity-wins` after changing entity files to sync to meta session
