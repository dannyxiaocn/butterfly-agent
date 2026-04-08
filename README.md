# Nutshell `v1.3.76`

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

nutshell server
nutshell chat "Plan a data pipeline"
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
tests/             automated coverage for runtime, CLI, providers, tools
```

Every active subsystem directory has its own `README.md`.

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
    tasks.md
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
```

## Reading Order

- [nutshell/README.md](/Users/xiaobocheng/agent_core/nutshell/nutshell/README.md)
- [entity/README.md](/Users/xiaobocheng/agent_core/nutshell/entity/README.md)
- [ui/README.md](/Users/xiaobocheng/agent_core/nutshell/ui/README.md)
- [tests/README.md](/Users/xiaobocheng/agent_core/nutshell/tests/README.md)

