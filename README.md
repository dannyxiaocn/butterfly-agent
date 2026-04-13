# Nutshell `v1.3.86`

Nutshell is a file-backed Python agent runtime. Sessions, prompts, tools, skills, state, and UI traffic all live on disk, so the server, CLI, Web UI, and agents share the same source of truth.

## Quick Start

```bash
pip install -e .

# Optional, but recommended for the default base entity:
codex login

# Optional provider/tool credentials:
export ANTHROPIC_API_KEY=...
export OPENAI_API_KEY=...
export KIMI_FOR_CODING_API_KEY=...
export BRAVE_API_KEY=...

nutshell-server              # auto-daemonizes; or nutshell-server --foreground
nutshell chat "Plan a data pipeline"   # auto-starts server if not running
```

The default `entity/agent` template uses `codex-oauth` with `gpt-5.4`. Other entities or sessions can switch provider and model in `core/params.json`.

## What The Repo Contains

```text
nutshell/
  core/            core agent abstractions and run loop
  llm_engine/      provider adapters and registry
  tool_engine/     tool loading, built-ins, executors, hot reload
  skill_engine/    SKILL.md loading and prompt rendering
  session_engine/  entity -> meta -> session materialization
  runtime/         watcher, IPC, bridge, coordination

ui/
  cli/             `nutshell` command-line entrypoints
  web/             FastAPI + SSE monitoring UI

entity/            built-in agent templates
tests/             automated coverage mirroring source tree layout
```

Detailed documentation for every subsystem lives in `docs/` — see [Documentation](#documentation) below.

## How It Works

1. An entity in `entity/<name>/` defines prompts, tools, skills, defaults, and optional memory.
2. `session_engine` creates `sessions/<id>/` and `_sessions/<id>/` from that entity.
3. `runtime` watches `_sessions/`, starts daemons, and drives heartbeat execution.
4. `Session` reloads prompts, memory, tools, and skills from `core/` before each activation.
5. CLI and Web clients communicate with the daemon by appending JSONL events to session files.

## Session Layout

```text
sessions/<id>/                  agent-visible
  core/
    system.md
    heartbeat.md
    session.md
    memory.md
    memory/*.md
    apps/*.md
    tasks/*.json
    params.json
    tools/*.json + *.sh
    skills/<name>/SKILL.md
  docs/
  playground/
  .venv/

_sessions/<id>/                 system-only
  manifest.json
  status.json
  context.jsonl
  events.jsonl
```

## Common CLI

```bash
nutshell chat "message"
nutshell chat --session <id> "continue"
nutshell new --entity nutshell_dev
nutshell sessions
nutshell log <id>
nutshell tasks <id>
nutshell stop <id>
nutshell start <id>
nutshell meta nutshell_dev
nutshell web

nutshell-server                # start server (auto-daemonize)
nutshell-server stop           # stop running server
nutshell-server status         # check if server is running
nutshell-server update         # reinstall package + restart
nutshell-server --foreground   # run in foreground (no daemonize)
```

## Documentation

All component documentation lives in `docs/`, mirroring the code structure. Each sub-directory contains three standard files:

| File | Purpose |
|------|---------|
| `design.md` | Component design, architecture, and rationale. Agents read this to understand intent; write back after implementing new designs. Keep concise. |
| `impl.md` | Implementation details: files, APIs, usage examples, important behaviors. The reference manual. |
| `todo.md` | Work log and tracking: completed work (with commit IDs), known bugs, future directions. |

```text
docs/
  nutshell/                      the Python runtime package
    core/                        agent loop, types, provider interface
    llm_engine/                  LLM provider adapters
      providers/                 per-vendor adapter details
    runtime/                     watcher, IPC, bridge, coordination
    service/                     shared service layer (CLI + Web)
    session_engine/              entity → meta → session lifecycle
    skill_engine/                skill loading and rendering
    tool_engine/                 tool loading and executors
      executor/                  concrete tool runtimes
        skill/                   built-in skill tool
        terminal/                shell execution backends
        web_search/              search provider backends
  entity/                        agent templates
    agent/                       base entity
      prompts/ tools/ skills/
    nutshell_dev/                project dev entity
      prompts/ memory/ skills/
    nutshell_dev_codex/          Codex variant
      memory/
  ui/                            user interfaces
    cli/                         command-line interface
    web/                         web UI + API
  tests/                         test infrastructure (mirrors source layout)
    nutshell/                    nutshell subsystem tests
    entity/                      entity contract tests
    ui/                          UI layer tests
    integration/                 cross-component tests
```

Conventions:

- **Agents** should read `design.md` before working on a component and update it after implementing significant changes.
- **`impl.md`** is the source of truth for "how does this work" and "how do I use it".
- **`todo.md`** replaces inline task tracking. Link commit IDs, note bugs, and plan future work here.
- Deeper directories inherit context from their parent’s docs — no need to repeat shared information.

Start here:

- [docs/nutshell/design.md](docs/nutshell/design.md) — architecture and design principles
- [docs/nutshell/impl.md](docs/nutshell/impl.md) — implementation details and session lifecycle
- [docs/entity/design.md](docs/entity/design.md) — entity template system
- [docs/ui/design.md](docs/ui/design.md) — CLI and Web frontends
- [docs/tests/design.md](docs/tests/design.md) — test infrastructure
