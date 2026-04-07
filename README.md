# Nutshell `v1.3.73`

A minimal Python agent runtime. Agents run as persistent server-managed sessions with autonomous heartbeat ticking.

---

## Quick Start

```bash
pip install -e .
export ANTHROPIC_API_KEY=...        # required
export BRAVE_API_KEY=...            # optional: web_search
codex login                         # optional: enables codex-oauth provider (default)

nutshell server                     # keep running in a terminal
nutshell chat "Plan a data pipeline"
```

---

## Architecture

```
nutshell/
├── core/          Agent loop, Tool, Skill, Provider ABC, Hook types
├── llm_engine/    LLM providers: anthropic, openai, kimi, codex  → README
├── tool_engine/   Bash executor, web_search, built-in tools, sandbox
├── skill_engine/  SKILL.md loader + system-prompt renderer
└── runtime/       Session lifecycle, IPC, watcher, meta-session   → README

ui/
├── cli/main.py    Unified `nutshell` CLI
└── web/           FastAPI + SSE monitoring UI at :8080

entity/            Entity definitions (agent.yaml + prompts/ + tools/ + skills/)  → README
tests/                                                                             → README
```

Each subdirectory has its own `README.md` with detailed documentation.

---

## Core Concepts

**Filesystem-As-Everything** — server and UI communicate only through files. No sockets.

**Session** — a running agent instance. Lives in `sessions/<id>/` (agent-visible) and `_sessions/<id>/` (system-only).

**Entity** — a reusable agent template in `entity/<name>/`. Read-only config; entities inherit via `extends`.

**Meta-session** — `sessions/<entity>_meta/` holds entity-level mutable state (shared memory, playground). Seeds all new child sessions.

**Heartbeat** — the server fires a periodic tick; agents read `core/tasks.md`, do work, go dormant. Non-empty task board keeps the cycle alive.

---

## Disk Layout

```
sessions/<id>/           ← agent-visible
  core/
    system.md            system prompt
    heartbeat.md         heartbeat prompt
    memory.md            persistent memory (auto-injected)
    memory/              named memory layers (*.md, loaded as-is each activation)
    tasks.md             task board — non-empty triggers next heartbeat
    params.json          explicit runtime config (model, provider, thinking, ...)
    tools/  skills/      agent-created tools and skills
  playground/            agent's free workspace

_sessions/<id>/          ← system-only
  manifest.json          entity, created_at
  status.json            model_state, pid, status
  context.jsonl          conversation history
  events.jsonl           streaming / runtime events
```

---

## CLI

```bash
# Messaging
nutshell chat "message"                          # new session
nutshell chat --entity nutshell_dev "message"    # custom entity
nutshell chat --session <id> "message"           # continue session

# Sessions
nutshell sessions / friends / kanban             # list / status / task board
nutshell new [ID] [--entity NAME]
nutshell stop / start SESSION_ID
nutshell log SESSION_ID [-n N] [--watch]
nutshell tasks SESSION_ID

# Entity
nutshell entity new [-n NAME] [--extends PARENT]

# Meta / dream
nutshell meta [ENTITY]                           # show meta session
nutshell meta ENTITY --init                      # re-run gene commands
nutshell dream ENTITY                            # wake meta session

# Server
nutshell server                                  # watcher + web UI
nutshell web                                     # web UI only
nutshell review                                  # review pending entity updates
```

---

## LLM Providers

| Key | Provider | Auth |
|-----|----------|------|
| `codex-oauth` | CodexProvider (gpt-5.4, default) | `codex login` → `~/.codex/auth.json` |
| `anthropic` | AnthropicProvider | `ANTHROPIC_API_KEY` |
| `openai` | OpenAIProvider | `OPENAI_API_KEY` |
| `kimi-coding-plan` | KimiForCodingProvider | `KIMI_FOR_CODING_API_KEY` |

Switch explicitly via `sessions/<id>/core/params.json`:
```json
{"provider": "anthropic", "model": "claude-opus-4-6", "thinking": true}
```

See `nutshell/llm_engine/README.md` for full provider docs including thinking/effort config.

---

## Testing

```bash
pytest tests/ -q
pytest tests/runtime/ -q  # runtime only
```

---

## Agent's Perspective — Improvement Notes

> Maintained by nutshell_dev agents. Surprising friction points recorded here.

- **No tool result visibility in web UI** — tool results require reading `context.jsonl` manually.
- **Memory layer truncation invisible to agent** — agent only gets a bash hint when layers are cut.
- **Playground push fails to non-bare remotes** — `receive.denyCurrentBranch` requires manual fetch+merge by orchestrator.
- **No structured tool responses** — tools return free-form strings; `{ok, output, error}` would help agents reason about failures.
