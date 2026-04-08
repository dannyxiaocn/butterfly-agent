---
name: nutshell
description: >
  Development context for the Nutshell repository. Use this skill for tasks that
  change Nutshell itself: runtime work, provider work, session lifecycle changes,
  CLI or web changes, entity updates, documentation updates, tests, or repo-level
  maintenance. Load it whenever the task is about this repository rather than an
  arbitrary external project.
---

# Nutshell Developer Skill

This skill gives the operational context for working on the Nutshell codebase.

## Working Rules

- Prefer reading the code and tests before assuming the docs are correct.
- Keep changes local to the current repo checkout unless the task explicitly says otherwise.
- When you change behavior, update the nearby docs and tests in the same pass.
- Use the unified `nutshell` CLI names that exist today. Do not rely on legacy command names.

## Repo Shape

```text
nutshell/          runtime implementation
ui/                CLI and FastAPI/SSE frontend
entity/            agent templates
tests/             automated coverage
README.md          repo-level overview
pyproject.toml     package metadata and CLI entrypoint
track.md           project task board
```

Important package boundaries:

- `nutshell/core`: `Agent`, `Tool`, `Skill`, `Provider`, shared types
- `nutshell/llm_engine`: provider registry and adapters
- `nutshell/tool_engine`: tool loading, built-ins, executors, hot reload
- `nutshell/skill_engine`: `SKILL.md` loading and rendering
- `nutshell/session_engine`: entity loading, session initialization, meta-session state, runtime session logic
- `nutshell/runtime`: watcher, IPC, bridge, coordination, `.env` loading
- `ui/cli`: the `nutshell` command
- `ui/web`: FastAPI app and browser UI

## Current Session Model

Nutshell is filesystem-first:

- `entity/<name>/` is the reusable template
- `sessions/<id>/` is the agent-visible runtime copy
- `_sessions/<id>/` is the system-visible runtime state
- `sessions/<entity>_meta/` is the mutable shared seed for future sessions of that entity

Key runtime files:

- `sessions/<id>/core/tasks.md`: heartbeat queue
- `sessions/<id>/core/params.json`: provider, model, heartbeat, tool-provider overrides, session type
- `_sessions/<id>/context.jsonl`: `user_input` and `turn` records
- `_sessions/<id>/events.jsonl`: live runtime events for UI streaming

## Commands You Should Expect

```bash
nutshell chat "message"
nutshell chat --entity nutshell_dev "message"
nutshell sessions
nutshell new [ID] [--entity NAME]
nutshell stop <id>
nutshell start <id>
nutshell log <id>
nutshell tasks <id>
nutshell visit <id>
nutshell friends
nutshell kanban
nutshell prompt-stats <id>
nutshell token-report <id>
nutshell repo-skill <path>
nutshell dream <entity>
nutshell meta [entity]
nutshell entity new -n <name>
nutshell server
nutshell web
```

## When Editing The Runtime

### Adding or changing a built-in tool

1. Implement the runtime behavior under `nutshell/tool_engine/`
2. Register or update it in `nutshell/tool_engine/registry.py` if it is a built-in
3. Update entity-exposed schemas in `entity/agent/tools/` if the base entity should expose it
4. Update docs and tests

### Adding or changing a provider

1. Update or add a file under `nutshell/llm_engine/providers/`
2. Register the provider key in `nutshell/llm_engine/registry.py`
3. Keep the provider contract aligned with `nutshell/core/provider.py`
4. Verify message conversion, tool calls, streaming, and token usage

### Adding or changing session behavior

Start by checking all of:

- `nutshell/session_engine/session.py`
- `nutshell/session_engine/session_init.py`
- `nutshell/session_engine/entity_state.py`
- `nutshell/runtime/watcher.py`
- the relevant tests in `tests/` and `tests/runtime/`

Session bugs often cross these boundaries.

## Testing

Run the smallest meaningful scope first, then widen if needed.

```bash
pytest tests/test_<module>.py -q
pytest tests/runtime/ -q
pytest tests/ -q
```

Common high-signal modules:

- `tests/test_tools.py`
- `tests/test_skill_tool.py`
- `tests/test_session_capabilities.py`
- `tests/test_persistent_mode.py`
- `tests/test_cli_main.py`
- `tests/runtime/test_meta_session.py`
- `tests/runtime/test_session_factory.py`

## Documentation Expectations

- Keep `README.md` files concise and implementation-faithful.
- Prefer explaining what a directory is, how to use it, and how it contributes to the system.
- Remove stale version counts, command names, or file references instead of preserving them for history.

## Practical Heuristics

- If a README and the code disagree, trust the code and fix the README.
- If a directory is an operational subsystem, it should have a short README.
- If a file path is part of a contract, mention the path exactly as the code uses it.
