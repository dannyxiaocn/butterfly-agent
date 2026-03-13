# Nutshell `v0.6.0`

A minimal Python agent runtime. Agents run as persistent server-managed sessions with autonomous heartbeat ticking, accessible via web browser.

---

## How It Works

```
nutshell-server    ← always-on process (manages all sessions)
nutshell-web       ← web UI at http://localhost:8080
```

Everything is files. The server and UI communicate only through files on disk — no sockets, no shared memory. You can kill the UI, restart the server, and sessions resume exactly where they left off.

---

## Quick Start

```bash
pip install -e .
export ANTHROPIC_API_KEY=sk-...

nutshell-server    # terminal 1: keep running
nutshell-web       # terminal 2: open http://localhost:8080
```

To scaffold a new agent entity:

```bash
nutshell-new-agent -n my-agent
```

---

## Filesystem as Everything

Nutshell's core design principle: **all state lives on disk**. There are two kinds of directories — `entity/` (agent definitions) and `sessions/` (live runtime state). The server reads both; nothing else is needed.

### Entity — Agent Definition

An entity is a static agent definition. It never changes at runtime.

```
entity/<name>/
├── agent.yaml              ← name, model, provider, tools, skills, prompts
├── prompts/
│   ├── system.md           ← agent identity and rules
│   └── heartbeat.md        ← injected into every heartbeat prompt (optional)
├── skills/
│   └── *.md                ← YAML frontmatter + body, injected into system prompt
└── tools/
    └── *.json              ← JSON Schema tool definitions
```

`agent.yaml` is the manifest:

```yaml
name: my-agent
model: claude-sonnet-4-6
provider: anthropic           # anthropic | kimi-coding-plan
release_policy: persistent
max_iterations: 20

prompts:
  system: prompts/system.md
  heartbeat: prompts/heartbeat.md

skills:
  - skills/coding.md

tools:
  - tools/bash.json
```

Multiple sessions can run from the same entity simultaneously.

---

### Session — Live Runtime State

A session is a running instance of an entity. Each session gets its own directory:

```
sessions/<id>/
├── params.json             ← runtime overrides: model, provider, heartbeat_interval
├── tasks.md                ← task board (agent reads/writes via injected tools)
├── files/                  ← attached files
├── prompts/
│   └── memory.md           ← agent persistent memory (auto-appended to system prompt)
├── skills/
│   └── *.md                ← per-session skill overrides (merged with entity skills)
└── _system_log/
    ├── manifest.json       ← static: entity path, created_at (written once, never mutated)
    ├── status.json         ← dynamic: model_state, pid, stopped/active, last_run_at
    ├── context.jsonl       ← append-only conversation history: user_input + turn events
    └── events.jsonl        ← runtime/UI events: streaming, status, errors
```

**Key invariants:**
- `_system_log/manifest.json` is immutable — written once at session creation.
- `params.json` is the source of truth for model, provider, heartbeat_interval.
- `_system_log/context.jsonl` is the sole source for conversation history — append-only, never rewritten.

---

### Config Loading — How It All Fits Together

On every activation (user message or heartbeat tick), the server reloads capabilities fresh from disk in this order:

```
entity/agent.yaml           → baseline model, provider, tools, skills, system prompt
        ↓
sessions/<id>/params.json   → override model and provider (agent can edit via bash)
        ↓
sessions/<id>/prompts/memory.md   → appended to system prompt
        ↓
sessions/<id>/skills/*.md   → merged with entity skills (session overrides by name)
```

This means agents can **modify their own runtime configuration** by writing to files in their session directory — changing model, provider, heartbeat interval, memory, or skills — all without server restart.

`params.json` schema:

```json
{
  "heartbeat_interval": 600.0,
  "model": null,       // null → use agent.yaml default
  "provider": null     // null → use agent.yaml default
}
```

---

### IPC — How Server and UI Communicate

All IPC is file-based. Two append-only logs per session:

**`context.jsonl`** — pure conversation history:

| Event type | Written by | Description |
|-----------|-----------|-------------|
| `user_input` | UI | User message |
| `turn` | Server | Completed agent turn (full Anthropic-format messages + tool calls) |

**`events.jsonl`** — runtime/UI signalling:

| Event type | Written by | Description |
|-----------|-----------|-------------|
| `model_status` | Server | `{"state": "running|idle", "source": "user|heartbeat"}` |
| `partial_text` | Server | Streaming text chunk (skipped on history replay) |
| `tool_call` | Server | Tool invocation before execution |
| `heartbeat_trigger` | Server | Written before heartbeat run starts |
| `heartbeat_finished` | Server | Agent signalled `SESSION_FINISHED` |
| `status` | Server | Session status changes (resumed, cancelled) |
| `error` | Server | Runtime errors |

The web UI polls both files via SSE. On reconnect it resumes from the last byte offset — no messages are lost, no full reload needed.

---

## Defining an Agent

### `prompts/system.md`

The agent's identity and rules. Include task board instructions if using heartbeat:

```markdown
You are a focused coding assistant.

## Task Board
Use read_tasks to check outstanding tasks.
Use write_tasks to update the board after each activation.
Call write_tasks("") when all work is done.
```

### `prompts/heartbeat.md`

Injected into every heartbeat prompt. If omitted, a generic fallback is used:

```markdown
Continue working on your tasks. When all tasks are done, respond with: SESSION_FINISHED
```

### `skills/*.md`

Skills inject context into the system prompt:

```markdown
---
name: coding
description: Expert coding practices
---

Always write type-annotated Python. Prefer composition over inheritance.
```

### `tools/*.json`

Tool schemas in Anthropic JSON Schema format. Built-in tools are auto-wired by name — just declare them in `agent.yaml`, no Python needed.

**Built-in: `bash`**

```json
{
  "name": "bash",
  "description": "Execute a shell command.",
  "input_schema": {
    "type": "object",
    "properties": {
      "command": { "type": "string" },
      "timeout": { "type": "number" },
      "workdir": { "type": "string" },
      "pty": { "type": "boolean" }
    },
    "required": ["command"]
  }
}
```

**Custom tools** — wire an implementation at load time:

```python
agent = AgentLoader(impl_registry={"search_web": my_search_fn}).load(Path("entity/my-agent"))
```

---

## Providers

| Provider key | Class | Auth env var |
|---|---|---|
| `anthropic` | `AnthropicProvider` | `ANTHROPIC_API_KEY` |
| `kimi-coding-plan` | `KimiForCodingProvider` | `KIMI_FOR_CODING_API_KEY` |

Set in `agent.yaml` (`provider: anthropic`) or override at runtime via `params.json`. Both providers use the Anthropic SDK — `kimi-coding-plan` points at Kimi's Anthropic-compatible endpoint (`https://api.kimi.com/coding/`).

---

## Web UI

```bash
nutshell-web                         # http://localhost:8080
nutshell-web --port 9000
nutshell-web --sessions-dir ~/sessions
```

**API:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/sessions` | List sessions |
| `POST` | `/api/sessions` | Create session |
| `GET` | `/api/sessions/{id}/history` | Full history + current offsets |
| `GET` | `/api/sessions/{id}/events?context_since=N&events_since=M` | SSE stream |
| `POST` | `/api/sessions/{id}/messages` | Send user message |
| `GET/PUT` | `/api/sessions/{id}/tasks` | Read/write task board |
| `POST` | `/api/sessions/{id}/stop\|start` | Pause/resume heartbeat |

---

## Project Structure

```
nutshell/
├── abstract/          # ABCs: BaseAgent, BaseTool, Provider, BaseLoader
├── core/
│   ├── agent.py       # Agent — LLM loop, tool execution, history management
│   ├── tool.py        # Tool + @tool decorator
│   ├── skill.py       # Skill dataclass
│   └── types.py       # Message, ToolCall, AgentResult
├── llm/
│   ├── anthropic.py   # AnthropicProvider (Anthropic SDK, supports custom base_url)
│   └── kimi.py        # KimiForCodingProvider (thin wrapper over AnthropicProvider)
├── runtime/
│   ├── session.py     # Session — persistent context + heartbeat daemon loop
│   ├── ipc.py         # FileIPC — context.jsonl + events.jsonl read/write
│   ├── status.py      # status.json read/write
│   ├── params.py      # params.json read/write
│   ├── provider_factory.py  # resolve provider by name, reverse-lookup
│   ├── watcher.py     # SessionWatcher — polls sessions/ directory
│   ├── server.py      # nutshell-server entry point
│   ├── loaders/
│   │   ├── agent.py   # AgentLoader: entity/ dir → Agent (reads agent.yaml)
│   │   ├── tool.py    # ToolLoader: .json → Tool (auto-wires built-ins)
│   │   └── skill.py   # SkillLoader: .md → Skill
│   └── tools/
│       ├── bash.py    # create_bash_tool(): subprocess + PTY execution
│       └── _registry.py  # Built-in tool registry (name → callable)
├── cli/
│   └── new_agent.py   # nutshell-new-agent: scaffold a new entity directory
└── ui/
    └── web.py         # nutshell-web (FastAPI + SSE, single-file server + HTML)
```

---

## TODO

### LLM

- **`thinking` block support** — `AnthropicProvider` and `KimiForCodingProvider` both silently discard `thinking` blocks returned by models that support extended thinking (e.g. `kimi-for-coding`, Claude with extended thinking). The reasoning process is never surfaced in the UI or stored in history. Fix: detect `block.type == "thinking"` in `complete()` and forward via a dedicated callback or prepend to `on_text_chunk`.

---

## Changelog

### v0.6.0
- **`provider` field in `agent.yaml`** — entity manifests now declare a `provider` (`anthropic`, `kimi-coding-plan`). `AgentLoader` resolves and sets `agent._provider` on load.
- **`nutshell-new-agent` CLI** — scaffolds a new entity directory with `agent.yaml`, `prompts/system.md`, `prompts/heartbeat.md`, `skills/`, and `tools/bash.json`.
- **Clean startup init order** — `watcher.py` uses provider/model from `agent.yaml` as baseline; `params.json` acts as override only when explicitly set. Actual values always written back so `params.json` reflects reality.
- **`KimiForCodingProvider`** — Anthropic-compatible provider for Kimi For Coding (`https://api.kimi.com/coding/`). Thin wrapper over `AnthropicProvider` with custom base URL.

### v0.5.9
- **params.json is the strict authority for model and provider** — at session startup, `watcher.py` applies `params.json` values before running. Actual resolved values written back so `params.json` always shows what is running.

### v0.5.8
- **Session directory reorganization** — system internals (`manifest.json`, `status.json`, `context.jsonl`, `events.jsonl`) moved into `_system_log/`. `params.json` promoted to session root. Agent-facing files now cleanly separated from system internals.

### v0.5.7
- **Layered capability management** — session gains `prompts/memory.md`, `skills/`, `params.json`. Agent edits its own memory, skills, model, provider, and heartbeat interval via bash.
- **Runtime provider switching** — `provider_factory.py` resolves provider by name. Setting `provider` in params.json switches provider on next activation without restart.

### v0.5.6
- **Long-running task awareness** — system prompt explains the heartbeat model. Dynamic wakeup scheduling via `write_tasks`.

### v0.5.5
- **Critical bugfix: `400 Extra inputs are not permitted`** — content blocks stored as plain copies without extra fields; `load_history()` runs allow-list cleaner on resume.

### v0.5.4
- **Editable heartbeat interval** — edit `sessions/<id>/status.json` to change interval; daemon reads it fresh each tick.

### v0.5.3
- **Context/events split** — `context.jsonl` is pure conversation history. Runtime/UI signalling moves to `events.jsonl`. SSE endpoint accepts separate offsets for both files.

### v0.5.2
- **Tool streaming** — `Agent.run()` accepts `on_tool_call`; tool invocations stream to UI before execution.
- **Heartbeat trigger ordering** — `heartbeat_trigger` written before run starts.

### v0.5.0
- **Streaming output** — `AnthropicProvider.complete()` accepts `on_text_chunk`; web UI shows real-time thinking bubble.
- **Markdown rendering** — agent messages rendered via `marked.js`.
- **Removed TUI** — all UI effort in web UI.

### v0.4.0
- **`Instance` → `Session`** — rename throughout. `kanban.md` → `tasks.md`.
- **Status-centric architecture** — `manifest.json` static, `status.json` dynamic.

### v0.3.0
- **Built-in `bash` tool** — `create_bash_tool()` factory, subprocess + PTY modes.

### v0.2.0
- **Single-file IPC** — `context.jsonl` replaces multiple files. Append-only.

### v0.1.0
- Initial release: server + web UI, persistent sessions, heartbeat, task board.
