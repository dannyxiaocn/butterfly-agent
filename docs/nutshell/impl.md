# Nutshell — Implementation

## Package Structure

```
nutshell/
  __init__.py           # Public exports
  core/                 # Pure computation: Agent loop, types, interfaces
  llm_engine/           # Provider adapters and registry
  tool_engine/          # Tool loading, executors, built-ins
  skill_engine/         # Skill loading and rendering
  session_engine/       # Entity → session lifecycle, Session wrapper
  runtime/              # Watcher, IPC, bridge, coordination
  service/              # Shared service layer for CLI + Web
```

## Entry Points (pyproject.toml)

| Command | Module | Purpose |
|---------|--------|---------|
| `nutshell` | `ui.cli.main:main` | Unified CLI |
| `nutshell-server` | `nutshell.runtime.server:main` | Standalone session daemon |
| `nutshell-web` | `ui.web.app:main` | FastAPI web UI |

## How a Session Runs

1. **Entity** defines agent template in `entity/<name>/`
2. **`session_engine.init_session()`** creates `sessions/<id>/` + `_sessions/<id>/` from entity via meta session
3. **`runtime.watcher`** discovers `_sessions/<id>/manifest.json`, starts daemon
4. **`Session`** reloads prompts, memory, tools, skills from `core/` before each activation
5. **`Agent.run()`** loops: build prompt → call LLM → execute tools → repeat
6. **`FileIPC`** persists all events to `context.jsonl` + `events.jsonl`
7. CLI/Web clients communicate by appending to / reading from these JSONL files

## Session Layout

```
sessions/<id>/              (agent-visible)
  core/
    system.md, heartbeat.md, session.md
    memory.md, memory/*.md
    apps/*.md
    params.json
    tools/*.json + *.sh
    skills/*, tasks/*.md
  docs/, playground/, .venv/

_sessions/<id>/             (system-only)
  manifest.json, status.json
  context.jsonl, events.jsonl
```
