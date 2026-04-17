# Butterfly — Implementation

## Package Structure

```
butterfly/
  __init__.py           # Public exports
  core/                 # Pure computation: Agent loop, types, interfaces
  llm_engine/           # Provider adapters and registry
  tool_engine/          # Tool loading, executors, built-ins
  skill_engine/         # Skill loading and rendering
  session_engine/       # Agent → session lifecycle, Session wrapper
  runtime/              # Watcher, IPC, bridge, coordination
  service/              # Shared service layer for CLI + Web
```

## Entry Points (pyproject.toml)

| Command | Module | Purpose |
|---------|--------|---------|
| `butterfly` | `ui.cli.main:main` | Unified CLI — subcommands cover sessions, agents, server lifecycle, auth, update |

v2.0.16 collapsed the separate server and web console scripts into the single `butterfly` entry point. The underlying modules (`butterfly.runtime.server`, `ui.web.app`) are still importable and are invoked internally: `_start_daemon` spawns `python -m butterfly.runtime.server --foreground`; `butterfly` (no-args) calls `ui.web.app.create_app()` and runs uvicorn in-process.

## How a Session Runs

1. **Agent** defines its template in `agenthub/<name>/`
2. **`session_engine.init_session()`** creates `sessions/<id>/` + `_sessions/<id>/` from agent via meta session
3. **`runtime.watcher`** discovers `_sessions/<id>/manifest.json`, starts daemon
4. **`Session`** reloads prompts, memory, tools, skills from `core/` before each activation
5. **`Agent.run()`** loops: build prompt → call LLM → execute tools → repeat
6. **`FileIPC`** persists all events to `context.jsonl` + `events.jsonl`
7. CLI/Web clients communicate by appending to / reading from these JSONL files

## Session Layout

```
sessions/<id>/              (agent-visible)
  core/
    system.md, task.md, env.md
    memory.md, memory/*.md
    apps/*.md
    config.yaml
    tools/*.json + *.sh
    skills/*, tasks/*.md
  docs/, playground/, .venv/

_sessions/<id>/             (system-only)
  manifest.json, status.json
  context.jsonl, events.jsonl
```
