---
name: butterfly
description: >
  Butterfly agent-runtime guide — covers both CLI usage (running agents,
  managing sessions, creating agents, viewing logs) and development on
  the Butterfly codebase itself (runtime, providers, session lifecycle,
  CLI/web changes, tool/skill engine, agent updates, tests, docs).
  Load this skill whenever the user asks how to use Butterfly from the
  terminal OR asks for work that changes Butterfly itself.
---

# Butterfly Agent — Usage and Development Guide

Butterfly is a minimal Python agent runtime. Agents persist across conversations via filesystem-based sessions. This skill covers two audiences:

1. **Using Butterfly** — run the CLI, drive agents, manage sessions (Part A).
2. **Developing Butterfly** — change the codebase itself (Part B).

Read the code and tests before trusting documentation. Keep changes local to the task scope. Update docs and tests together with behaviour changes.

---

## Part A — Using Butterfly (CLI guide)

### Core Concepts

- **Agent** — a reusable agent template (`agenthub/<name>/`): config, prompts, tools, skills
- **Session** — a running instance of an agent (`sessions/<id>/`): agent-visible workspace
- **Meta session** — mutable shared seed for all sessions of an agent (`sessions/<agent>_meta/`)
- **Server** — background daemon that watches for sessions and runs agent loops

### Quick Start

```bash
# Start server + web UI together (prints URL, hangs). Ctrl+C stops both.
butterfly

# Tail the running server's log in another terminal.
butterfly server

# Send a message (auto-starts server if needed)
butterfly chat "Hello, what can you do?"

# Use a specific agent
butterfly chat --agent butterfly_dev "Review the codebase"

# Continue an existing session
butterfly chat --session 2026-04-13_10-00-00-a1b2 "What's the status?"
```

### All CLI Commands

#### Session Interaction

| Command | Description |
|---------|-------------|
| `butterfly chat MESSAGE` | Send a message; creates a new session or continues one |
| `butterfly new [ID]` | Create a session without sending a message |
| `butterfly stop SESSION_ID` | Stop a session's heartbeat loop |
| `butterfly start SESSION_ID` | Resume a stopped session |

**`chat` flags:**
- `--session ID` — continue an existing session
- `--agent NAME` — agent to use (default: `agent`)
- `--no-wait` — fire-and-forget (don't block for reply)
- `--timeout N` — seconds to wait (default: 300)
- `--keep-alive` — keep server running after reply
- `--inject-memory KEY=VALUE` or `KEY=@FILE` — inject memory layers

**`new` flags:**
- `--agent NAME` — agent to init from (default: `agent`)
- `--heartbeat N` — heartbeat interval in seconds
- `--inject-memory KEY=VALUE` — inject memory at creation

#### Monitoring & Views

| Command | Description |
|---------|-------------|
| `butterfly sessions` | List all sessions with status |
| `butterfly log [SESSION_ID]` | Show conversation history |
| `butterfly tasks [SESSION_ID]` | Show task cards |

**`log` flags:**
- `-n N` — number of turns (default: 5)
- `--since TIMESTAMP` — filter by time (ISO-8601, epoch, or `now`)
- `--watch` — poll for new turns every 2s

#### Agent Management

| Command | Description |
|---------|-------------|
| `butterfly agent new` | Scaffold a new agent (interactive) |

**`agent new` flags:**
- `-n NAME` — skip interactive prompt
- `--init-from SOURCE` — copy from existing agent
- `--blank` — empty agent with placeholders

#### Server / Web Management (unified CLI, v2.0.16)

| Command | Description |
|---------|-------------|
| `butterfly` (no args) | Start server + web UI together; prints `http://localhost:7720`; blocks until Ctrl+C |
| `butterfly server` | Tail the running server's log (read-only; needs the daemon up) |
| `butterfly update [--skip-frontend]` | git pull + pip install -e . + npm build + restart (refuses dirty/untracked tree) |

Internal use: `python -m butterfly.runtime.server --foreground` is what `_start_daemon` spawns and what the auto-update path `execvp`s onto. You normally never invoke it directly.

### Session Lifecycle

```
agenthub/ ──create──> sessions/<id>/ ──chat──> agent runs ──stop──> napping
                                                  ↑                  │
                                                  └────start─────────┘
```

1. `butterfly new` or `butterfly chat` creates a session from an agent template
2. Server picks up the session and runs the agent loop
3. Agent reads/writes `core/` files (memory, tasks, tools, skills)
4. `butterfly stop` pauses; `butterfly start` resumes

### Practical Workflows

**Quick one-shot question:**
```bash
butterfly chat "Explain how Python generators work"
```

**Long-running dev session:**
```bash
butterfly new --agent butterfly_dev my-feature
butterfly chat --session my-feature "Add pagination to the API"
butterfly log --session my-feature --watch  # monitor progress
```

**Inject context into a session:**
```bash
butterfly chat --inject-memory spec=@design_doc.md "Implement this spec"
```

**Check all agent activity:**
```bash
butterfly sessions              # list all sessions with status
butterfly tasks <SESSION_ID>    # inspect a specific session's task board
```

---

## Part B — Developing Butterfly (contributor guide)

### Repo Layout

```text
butterfly/           runtime implementation
├── core/           Agent, Tool, Skill, Provider ABCs, types, BaseLoader
├── llm_engine/     provider registry + adapters (anthropic, openai, kimi, codex)
├── tool_engine/    tool loading, executors, registry
├── skill_engine/   SKILL.md loading + system prompt rendering
├── session_engine/ agent config, session init, meta-session state, task cards
└── runtime/        server, watcher, IPC, bridge, env, git coordination
toolhub/            built-in tool implementations (tool.json + executor.py)
skillhub/           built-in skill definitions (SKILL.md)
ui/
├── cli/            `butterfly` CLI entry point
└── web/            FastAPI + SSE + Vite frontend
agenthub/             agent templates (config.yaml + prompts/ + tools.md + skills.md)
tests/              mirrors source layout
docs/               documentation and task boards
```

### Key Design Principles

#### Filesystem-as-Everything
- Agents read/write session directories; IPC via `context.jsonl` + `events.jsonl`
- `agenthub/` is read-only template; all mutable state in `sessions/`
- `sessions/<agent>_meta/` holds agent-level mutable state

#### Hub Pattern (toolhub + skillhub)
- All built-in tools live in `toolhub/<name>/` with `tool.json` + `executor.py`
- All built-in skills live in `skillhub/<name>/SKILL.md`
- Agent and session `tools.md` / `skills.md` only list **enabled** names (one per line)
- Agent-created tools (`core/tools/`) and skills (`core/skills/`) are session-local extensions

#### Progressive Disclosure (Skills)
- File-backed skills render as `<available_skills>` catalog in system prompt
- Model loads full skill body on demand via the `skill` tool
- Inline skills (no file location) inject body directly

#### Dependency Flow
```
UI → runtime → session_engine → core
```
- `session_engine` never imports `runtime` (except `git_coordinator`)
- `core` should stay low-dependency, but currently depends on `llm_engine` and `skill_engine` in a few places

### Package Boundaries

| Package | Owns | Does NOT own |
|---------|------|-------------|
| `core/` | Agent loop, Tool/Skill/Provider dataclasses, types | Loading, lifecycle |
| `llm_engine/` | Provider implementations, message conversion | Tool execution |
| `tool_engine/` | ToolLoader, executor dispatch, shell/bash tools | Agent loop |
| `skill_engine/` | SkillLoader, skills.md parsing, prompt rendering | Tool execution |
| `session_engine/` | Session lifecycle, agent config, meta-session, task cards | Central dispatch |
| `runtime/` | Server, watcher, IPC, bridge | Agent config |

### Session Model

```
agenthub/<name>/           read-only template
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
      └── tasks/         task cards (*.json)

_sessions/<id>/          system-only twin (agent never sees)
  ├── manifest.json      agent name, created_at
  ├── status.json        dynamic runtime state
  ├── context.jsonl      conversation records
  └── events.jsonl       live runtime events for UI streaming
```

### How to Add Things

#### Adding a built-in tool

1. Create `toolhub/<name>/tool.json` (Anthropic tool schema format)
2. Create `toolhub/<name>/executor.py` with an executor class
3. Register special context injection in `butterfly/tool_engine/loader.py` `_create_executor()` if needed
4. Add the tool name to relevant agent `tools.md` files
5. Update docs and tests

#### Adding a provider

1. Create or update `butterfly/llm_engine/providers/<name>.py`
2. Register in `butterfly/llm_engine/registry.py`
3. Align with `butterfly/core/provider.py` contract
4. Verify: message conversion, tool calls, streaming, token usage

#### Adding a built-in skill

1. Create `skillhub/<name>/SKILL.md` with frontmatter + body
2. Add the skill name to relevant agent `skills.md` files
3. Update docs

#### Adding an agent

1. Create `agenthub/<name>/` with `config.yaml`, `prompts/`, `tools.md`, `skills.md` (or use `butterfly agent new -n <name> --init-from agent`)
2. The meta session is initialized automatically the first time a child session is created from the agent (`populate_meta_from_agent` runs during `init_session`).

### System Prompt Assembly

Order (top to bottom):
1. `system.md` — static system prompt (cached)
2. `env.md` — session environment context (cached)
3. `memory.md` — persistent memory
4. `memory/*.md` — named memory layers (truncated at 60 lines each)
5. App notifications (`core/apps/*.md`)
6. Skills catalog (`<available_skills>` block)

Static prefix (`system.md` + `env.md`) uses Anthropic `cache_control: ephemeral`.

### Testing

Run the smallest scope first:
```bash
pytest tests/butterfly/skill_engine/ -q
pytest tests/butterfly/session_engine/ -q
pytest tests/ -q
```

Test layout mirrors source: `tests/butterfly/{core,llm_engine,tool_engine,...}/`

### Practical Heuristics

- If a README and the code disagree, trust the code and fix the README
- If a directory is an operational subsystem, it should have a short README
- If a file path is part of a contract, mention the path exactly as the code uses it
- After changing files under `agenthub/<name>/`, either (a) delete the meta session `sessions/<name>_meta/` and `_sessions/<name>_meta/` so the next child session re-bootstraps from the agent, or (b) manually mirror the edits into `sessions/<name>_meta/core/` for existing sessions to pick up.
