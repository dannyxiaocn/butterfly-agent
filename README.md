# Nutshell `v0.5.5`

A minimal Python agent runtime. Agents run as persistent server-managed sessions with autonomous heartbeat ticking, accessible via web browser.

---

## How It Works

```
nutshell-server          ← always-on backend (manages all sessions)
nutshell-web             ← web UI (http://localhost:8080)
```

Server and UI communicate only through files — no sockets. You can open multiple browser tabs against the same server, attach and detach freely, and the server keeps running when you close the browser.

---

## Quick Start

```bash
pip install -e .
export ANTHROPIC_API_KEY=sk-...

nutshell-server                          # terminal 1: keep running
nutshell-web                             # terminal 2: web UI at http://localhost:8080
```

---

## Concepts

### Session

A **session** is a running agent instance — a specific agent entity loaded into a persistent context. Each session has its own directory:

```
sessions/my-project/
├── manifest.json    ← static config (entity, heartbeat interval, created_at) — written once
├── status.json      ← all dynamic state (model_state, pid, stopped/active, last_run_at)
├── tasks.md         ← task board (read/written by the agent)
├── context.jsonl    ← append-only event log: all conversation + IPC
└── files/           ← attached files
```

**Two-file state model**: `manifest.json` is immutable after creation. All runtime state — PID, running/idle, stopped/active, last run timestamp — lives in `status.json` and is updated continuously by the daemon.

`context.jsonl` is the single source of truth. It is strictly append-only. All events flow through it:

| Type | Written by | Description |
|------|-----------|-------------|
| `user_input` | UI | User message |
| `turn` | Server | Completed agent turn (full Anthropic-format messages) |
| `partial_text` | Server | Streaming text chunk (ephemeral — skipped on history replay) |
| `model_status` | Server | Model state change: `{"state": "running\|idle", "source": "user\|heartbeat"}` |
| `status` | Server | Session status changes (resumed, cancelled, heartbeat paused) |
| `error` | Server | Runtime errors |
| `heartbeat_finished` | Server | Agent signalled `SESSION_FINISHED` |

The UI derives display events (`user`, `agent`, `tool`, `heartbeat_trigger`) by parsing `turn` events. `model_status` events drive live status indicators — no polling required. `partial_text` events stream text chunks to the browser in real time during model output; they are skipped when replaying history.

### Entity

An **entity** is an agent definition — system prompt, model, tools, skills. Entities are plain files under `entity/`:

```
entity/my-agent/
├── agent.yaml
├── prompts/
│   ├── system.md       ← agent identity and rules
│   └── heartbeat.md    ← heartbeat behavior (optional)
├── skills/
│   └── coding.md       ← injected into system prompt (YAML frontmatter + body)
└── tools/
    └── search.json     ← tool schema (JSON Schema)
```

Multiple sessions can run from the same entity.

### Heartbeat

Every `heartbeat_interval` seconds (default: 10s), the server checks if `tasks.md` is non-empty. If so, the agent is invoked to continue work autonomously. The interval is counted **from when the previous tick completes**, so a slow agent never gets back-to-back ticks.

The agent signals completion by responding with `SESSION_FINISHED` — this clears the task board and ends the heartbeat cycle.

---

## Defining an Agent

### `agent.yaml`

```yaml
name: my-agent
model: claude-sonnet-4-6
release_policy: persistent   # persistent | auto | manual
max_iterations: 20

prompts:
  system: prompts/system.md
  heartbeat: prompts/heartbeat.md  # optional

skills:
  - skills/coding.md

tools:
  - tools/search.json
```

### `prompts/system.md`

The agent's identity and rules. Agents that use the task board should include task instructions:

```markdown
You are a focused coding assistant.

## Task Board

You have a persistent task board for tracking work across sessions.
Use read_tasks to check outstanding tasks.
Use write_tasks to update the board when you finish each activation:
- Remove completed tasks, leave unfinished ones with notes.
- Call write_tasks("") when all work is done.
An empty task board means no outstanding work remains.
```

### `prompts/heartbeat.md`

Instructions injected into every heartbeat prompt. If omitted, a generic fallback is used:

```markdown
When activated by the heartbeat, continue working on your tasks.
After each heartbeat:
- Call write_tasks("") when ALL tasks are finished.
- Update the task board with progress notes if work remains.
- Summarize what you did.

If all work is complete, respond with exactly: SESSION_FINISHED
```

### `skills/*.md`

Skills inject context into the system prompt via YAML frontmatter:

```markdown
---
name: coding
description: Expert coding practices
---

Always write type-annotated Python.
Prefer composition over inheritance.
```

### `tools/*.json`

Tool schemas in Anthropic JSON Schema format. Built-in tools (`bash`) are auto-wired — just declare them in `agent.yaml` and add their JSON schema, no Python wiring needed. Custom tools register their implementation via `AgentLoader(impl_registry={...})`.

**Built-in: `bash`**

Add `tools/bash.json` to your entity and list it in `agent.yaml`. The agent can then run shell commands:

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
      "pty":     { "type": "boolean", "description": "Run in a pseudo-terminal (preserves color, isatty). Unix only." }
    },
    "required": ["command"]
  }
}
```

Or use it directly in Python:

```python
from nutshell import Agent, create_bash_tool

agent = Agent(
    system_prompt="You are a coding assistant.",
    tools=[create_bash_tool(timeout=60, workdir="/my/project")],
)
result = await agent.run("Run the tests and show me the output")
```

**Custom tools**

```json
{
  "name": "search_web",
  "description": "Search the web.",
  "input_schema": {
    "type": "object",
    "properties": { "query": { "type": "string" } },
    "required": ["query"]
  }
}
```

Wire the implementation:

```python
agent = AgentLoader(impl_registry={"search_web": my_search_fn}).load(Path("entity/my-agent"))
```

---

## Web UI

```bash
nutshell-web                          # http://localhost:8080
nutshell-web --port 9000
nutshell-web --sessions-dir ~/my-sessions
```

3-column layout: session list (left, with live state dots and sorted by activity), chat with SSE streaming (center), task editor (right). Stop/Start buttons per session. Header shows current session name and live state indicator.

**Session states** (colour-coded in sidebar and header):

| State | Meaning |
|-------|---------|
| `running` | Agent actively generating (green, pulsing) |
| `tasks queued` | Tasks in `tasks.md`, heartbeat pending (yellow) |
| `idle` | No pending tasks (dim) |
| `stopped` | Heartbeat paused by user (red) |

**Streaming**: While the agent generates text, a pulsing "thinking" bubble appears in real time. Text chunks stream into it via `partial_text` SSE events. Tool calls appear at the end of each turn. On history load, partial chunks are skipped — only complete turns are replayed.

**Session ordering**: running > queued > idle > stopped. Within each group, most recently used sessions appear first.

**API:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/sessions` | List sessions |
| `POST` | `/api/sessions` | Create session |
| `GET` | `/api/sessions/{id}/history` | Full event history + current offset |
| `GET` | `/api/sessions/{id}/events?since=N` | SSE stream from byte offset |
| `POST` | `/api/sessions/{id}/messages` | Send user message |
| `GET/PUT` | `/api/sessions/{id}/tasks` | Read/write task board |
| `POST` | `/api/sessions/{id}/stop\|start` | Pause/resume heartbeat |

---

## Project Structure

```
nutshell/
├── abstract/          # ABCs: BaseAgent, BaseTool, Provider, BaseLoader
├── core/
│   ├── agent.py       # Agent — LLM loop, tool execution, history
│   ├── tool.py        # Tool + @tool decorator
│   ├── skill.py       # Skill dataclass
│   └── types.py       # Message, ToolCall, AgentResult
├── llm/
│   ├── anthropic.py   # AnthropicProvider (default)
│   └── openai.py      # OpenAIProvider
├── runtime/
│   ├── session.py     # Session — persistent context + heartbeat daemon loop
│   ├── ipc.py         # FileIPC — context.jsonl append + display event derivation
│   ├── status.py      # status.json read/write (live model state per session)
│   ├── watcher.py     # SessionWatcher — polls sessions/ directory
│   ├── server.py      # nutshell-server entry point
│   ├── loaders/
│   │   ├── agent.py   # AgentLoader: entity/ dir → Agent
│   │   ├── tool.py    # ToolLoader: .json → Tool (auto-wires built-ins)
│   │   └── skill.py   # SkillLoader: .md → Skill
│   └── tools/
│       ├── bash.py    # create_bash_tool(): subprocess + PTY execution
│       └── _registry.py  # Built-in tool registry (name → callable)
└── ui/
    └── web.py         # nutshell-web (FastAPI + SSE)
```

---

## Tests

```bash
pytest tests/    # uses MockProvider, no API key needed
```

---

## Changelog

### v0.5.5
- **Critical bugfix: `400 Extra inputs are not permitted`** — `_serialize_message_content` was injecting a `ts` field into every Anthropic content block (text, tool_use, tool_result). These blocks were stored in `context.jsonl` and reloaded into `_history` on session resume, causing the API to reject them. Fix: content blocks are now stored as plain copies without extra fields. `load_history()` also runs a allow-list cleaner (`_clean_content_for_api`) to strip any non-API fields from existing sessions on disk.

### v0.5.4
- **Editable heartbeat interval** — `heartbeat_interval` moved from `manifest.json` to `status.json`. Edit `sessions/<id>/status.json` directly to change the interval; the daemon reads it fresh each tick without restarting. Old sessions are auto-migrated on first start (manifest value copied to status.json). Tasks panel now shows `updated HH:MM · every Xm`.
- **watcher.py bugfix** — `ipc.append()` → `ipc.append_event()` (crashed on session error since v0.5.3 removed the old append API).

### v0.5.3
- **Context/events split** — `context.jsonl` is now a pure conversation log containing only `user_input` and `turn` events. All runtime/UI signalling (`model_status`, `partial_text`, `tool_call`, `heartbeat_trigger`, `heartbeat_finished`, `status`, `error`) moves to a new `events.jsonl`. The sole purpose of `context.jsonl` is to restore the full agent conversation history and send correct context to Claude on every run. `FileIPC` gains `append_context`/`append_event`, `tail_history`/`tail_context`/`tail_runtime_events`, and `events_size()`. History endpoint returns `{context_offset, events_offset}`; SSE endpoint accepts both offsets separately. Old sessions degrade gracefully (existing mixed `context.jsonl` still loads as history; absent `events.jsonl` silently skipped).

### v0.5.2
- **Tool streaming** — `Agent.run()` now accepts `on_tool_call` callback; each tool invocation is streamed to `context.jsonl` as a `tool_call` event before execution, so tools appear one by one in the UI with their own timestamps. Turns are marked `has_streaming_tools=True` to avoid duplicating tool events from history replay.
- **Heartbeat trigger ordering fix** — `heartbeat_trigger` event is now written to `context.jsonl` *before* the heartbeat run starts, so the "⏱ heartbeat — checking tasks" box appears in the UI before the thinking bubble (not after the agent turn completes).
- **User re-input from stopped state** — UI now optimistically transitions from red (stopped) to green (running) immediately when user sends a message, without waiting for the daemon poll cycle. Backend also reshapes history to remove orphaned trailing user messages (e.g., interrupted heartbeat prompts) before processing new input.
- **Tasks last update time** — `status.json` now tracks `tasks_updated_at`; displayed in the tasks panel header whenever tasks are written (by agent or by user).
- **Heartbeat interval** — default changed from 10 seconds to 10 minutes (600s).

### v0.5.0
- **Status-centric architecture** — `manifest.json` is now purely static config (written once at creation, never mutated). All dynamic runtime state (`pid`, `status`, `model_state`, `last_run_at`) lives in `status.json` and is updated continuously by the daemon. Eliminates the mixed-paradigm where half the state was in manifest and half in status.
- **Streaming output** — `AnthropicProvider.complete()` now accepts an `on_text_chunk` callback; when provided it uses the Anthropic streaming API and emits `partial_text` events to `context.jsonl`. The web UI shows a real-time "thinking" bubble that fills with streamed text as it arrives.
- **Markdown rendering** — Agent messages are rendered as Markdown (via `marked.js`): code blocks, lists, tables, headings, inline code, blockquotes.
- **Session sorting** — Sessions in the sidebar are ordered by activity: running > tasks queued > idle > stopped, then by most-recently-run timestamp (most recent first), then by creation time.
- **Removed TUI** — `nutshell-tui` and `textual` dependency removed. All UI effort goes into the web UI.
- **`Provider.complete()` signature** — added `on_text_chunk: Callable[[str], None] | None = None` keyword argument to the abstract interface (backward compatible with `None` default).

### v0.4.0
- **Restructured package layout** — `loaders/` and `tools/` moved into `runtime/` sub-packages; `infra/` renamed to `runtime/`. The package now has four clear layers: `abstract` (interfaces), `core` (pure agent logic), `llm` (providers), `runtime` (server + loaders + built-in tools), `ui` (interfaces).
- **`Instance` → `Session`** — the persistent run context class is now `Session`; `INSTANCE_FINISHED` → `SESSION_FINISHED`; default storage directory `instances/` → `sessions/`.
- **`kanban.md` → `tasks.md`** — the per-session task board file is renamed; injected agent tools renamed `read_tasks` / `write_tasks`; API routes `/kanban` → `/tasks`; TUI command `/kanban` → `/tasks`.
- **Live model status** — `Session` emits `model_status` events (`running`/`idle`, source `user`/`heartbeat`) around every agent run; also writes `status.json` per session so UIs can poll the live state.
- **Redesigned TUI** — ListView-based session sidebar with four-state indicators (running/tasks queued/idle/stopped); live `StatusBar`; keyboard shortcuts (`Ctrl+S` stop, `Ctrl+G` start, `Ctrl+J` sessions, `Ctrl+L` input); Markdown rendering for agent messages.
- **Redesigned Web UI** — session state dots with colour coding; header shows current session name and live state indicator; `model_status` SSE events update the indicator in real time without polling.
- **Removed `echo` built-in from `agent_core`** — was a test-only tool with no practical use in the default agent.
- **Removed duplicate `providers/` package** — was an exact copy of `llm/`, dead code.

### v0.3.0
- **Built-in `bash` tool** — `create_bash_tool()` factory returns a Tool agents can use to run shell commands. Two execution modes: async subprocess (default) and PTY (`pty=True`, preserves `isatty()` / color output, Unix only via stdlib `pty` + reader thread pattern).
- **Built-in tool registry** — `_registry.py` maps tool names to implementations. `ToolLoader` falls back to this registry automatically, so entities can declare `bash` in `agent.yaml` without any Python wiring.
- **`entity/agent_core` gains `bash` tool** — `tools/bash.json` added and registered in `agent.yaml`.
- **`create_bash_tool` exported** from `nutshell` top-level.

### v0.2.0
- **Single-file IPC** — `context.jsonl` replaces `inbox.jsonl`, `outbox.jsonl`, and `daemon.pid`. Session directory reduced from 6 files to 3.
- **Append-only context** — every write is O(1); no more read-modify-write JSON array.
- **PID in manifest** — `daemon.pid` eliminated; PID stored in `manifest.json["pid"]`.
- **Heartbeat prompt in entity** — behavior instructions moved from hardcoded Python to `prompts/heartbeat.md`, configurable per agent via `agent.yaml`.
- **Dead code removed** — `BaseSkill`, `PromptLoader`, `Skill.to_prompt_fragment()`, `Instance.is_done()`, `Instance.close()`, `.nutshell_log`.

### v0.1.1
- History resume, lossless context storage, inbox replay prevention, heartbeat ghost output fix, stop/start indicator, crashed session restart.

### v0.1.0
- Initial release: server + TUI + web UI, persistent sessions, heartbeat, task board.
