# Nutshell `v0.9.2`

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
export BRAVE_API_KEY=...       # optional: enables web_search tool (default provider)
export TAVILY_API_KEY=...      # optional: enables web_search via Tavily provider

nutshell-server    # terminal 1: keep running
nutshell-web       # terminal 2: open http://localhost:8080
```

To scaffold a new agent entity (inherits from `agent_core` by default):

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

`agent.yaml` is the manifest. All fields are always present:

```yaml
name: my-agent
description: ""
model: claude-sonnet-4-6
provider: anthropic           # anthropic | kimi-coding-plan
release_policy: persistent    # persistent | auto | manual
max_iterations: 20

prompts:
  system: prompts/system.md
  heartbeat: prompts/heartbeat.md
  session_context: prompts/session_context.md

skills:
  - skills/coding.md

tools:
  - tools/bash.json
  - tools/web_search.json
```

Multiple sessions can run from the same entity simultaneously.

### Entity Inheritance

Entities can extend a parent entity with `extends: parent_name`. `agent.yaml` always declares all fields — a **null value** means "inherit from parent":

```yaml
name: my-agent
description: ""
model: claude-sonnet-4-6
provider: anthropic
extends: agent_core           # parent entity
release_policy: persistent
max_iterations: 20

prompts:
  system:           # null → load from agent_core/prompts/system.md
  heartbeat:        # null → inherit
  session_context: prompts/session_context.md  # own file → load from here

tools:    # null → inherit parent's full tools list
skills:   # null → inherit parent's full skills list
```

**Rules:**
- **Prompts** — null value → load file from parent's directory. String value → load from this entity's directory.
- **tools / skills** — null (or absent) → inherit parent's entire list; each file resolves child-first, then parent-fallback. `[]` → explicitly empty (no inheritance).
- **Scalar fields** (model, provider, etc.) — always taken from this entity's `agent.yaml`.

When you override a single tool or skill, list all paths you want; files missing in the child directory fall back to the parent's copy automatically.

`nutshell-new-agent` creates a minimal inheriting entity by default — full `agent.yaml`, empty `prompts/`/`tools/`/`skills/` dirs (with placeholder files), everything inherited from `agent_core`:

```bash
nutshell-new-agent -n my-agent                   # extends agent_core (default)
nutshell-new-agent -n my-agent --extends kimi_core
nutshell-new-agent -n my-agent --no-inherit      # standalone, copies agent_core files
```

---

### Session — Live Runtime State

A session is a running instance of an entity. Each session gets its own directory:

```
sessions/<id>/
├── params.json             ← runtime overrides: model, provider, heartbeat_interval, tool_providers
├── tasks.md                ← task board (agent reads/writes via bash)
├── files/                  ← attached files
├── prompts/
│   └── memory.md           ← agent persistent memory (auto-appended to system prompt)
├── skills/
│   └── *.md                ← per-session skill overrides (merged with entity skills)
├── tools/
│   ├── my_tool.json        ← agent-created tool schema
│   └── my_tool.sh          ← agent-created tool implementation (bash, reads JSON from stdin)
└── _system_log/
    ├── manifest.json       ← static: entity path, created_at (written once, never mutated)
    ├── status.json         ← dynamic: model_state, pid, stopped/active, last_run_at
    ├── context.jsonl       ← append-only conversation history: user_input + turn events
    └── events.jsonl        ← runtime/UI events: streaming, status, errors
```

**Key invariants:**
- `_system_log/manifest.json` is immutable — written once at session creation.
- `params.json` is the source of truth for model, provider, heartbeat_interval, and tool_providers.
- `_system_log/context.jsonl` is the sole source for conversation history — append-only, never rewritten.

---

### Config Loading — How It All Fits Together

On every activation (user message or heartbeat tick), the server reloads capabilities fresh from disk in this order:

```
entity/agent.yaml                   → baseline model, provider, tools, skills, system prompt
        ↓
sessions/<id>/params.json           → override model, provider, heartbeat_interval, tool_providers
        ↓
sessions/<id>/prompts/memory.md     → appended to system prompt
        ↓
sessions/<id>/skills/*.md           → merged with entity skills (session overrides by name)
        ↓
sessions/<id>/tools/*.json + *.sh   → session-scoped tools (agent-created, loaded last)
```

This means agents can **modify their own runtime configuration** by writing to files in their session directory — changing model, provider, heartbeat interval, memory, skills, or tools — all without server restart.

`params.json` schema:

```json
{
  "heartbeat_interval": 600.0,
  "model": null,           // null → use agent.yaml default
  "provider": null,        // null → use agent.yaml default
  "tool_providers": {}     // e.g. {"web_search": "tavily"} — empty = use built-in defaults
}
```

---

## Defining an Agent

### `prompts/system.md`

The agent's identity and rules. Include task board instructions if using heartbeat:

```markdown
You are a focused coding assistant.

## Task Board
Read and write `sessions/YOUR_ID/tasks.md` via bash.
Clear the file when all work is done.
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

**Built-in: `web_search`** — Pluggable web search. Default provider: Brave (`BRAVE_API_KEY`). Switch to Tavily (`TAVILY_API_KEY`) by setting `tool_providers: {"web_search": "tavily"}` in `params.json`.

```json
{
  "name": "web_search",
  "description": "Search the web using Brave Search.",
  "input_schema": {
    "type": "object",
    "properties": {
      "query":       { "type": "string" },
      "count":       { "type": "number", "description": "1-10, default 5" },
      "country":     { "type": "string", "description": "2-letter country code" },
      "language":    { "type": "string", "description": "ISO 639-1 code" },
      "freshness":   { "type": "string", "description": "day | week | month | year" },
      "date_after":  { "type": "string", "description": "YYYY-MM-DD" },
      "date_before": { "type": "string", "description": "YYYY-MM-DD" }
    },
    "required": ["query"]
  }
}
```

**Custom tools** — wire an implementation at load time:

```python
agent = AgentLoader(impl_registry={"my_tool": my_fn}).load(Path("entity/my-agent"))
```

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
├── providers/
│   ├── llm/
│   │   ├── anthropic.py   # AnthropicProvider (Anthropic SDK, supports custom base_url)
│   │   └── kimi.py        # KimiForCodingProvider (thin wrapper over AnthropicProvider)
│   └── tool/
│       ├── web_search.py  # create_web_search_tool() — Brave Search (default)
│       └── tavily.py      # create_web_search_tool() — Tavily Search
├── runtime/
│   ├── session.py     # Session — persistent context + heartbeat daemon loop
│   ├── ipc.py         # FileIPC — context.jsonl + events.jsonl read/write
│   ├── status.py      # status.json read/write
│   ├── params.py      # params.json read/write
│   ├── provider_factory.py      # resolve LLM provider by name, reverse-lookup
│   ├── tool_provider_factory.py # resolve tool impl by (tool_name, provider_name)
│   ├── watcher.py     # SessionWatcher — polls sessions/ directory
│   ├── server.py      # nutshell-server entry point
│   ├── loaders/
│   │   ├── agent.py   # AgentLoader: entity/ dir → Agent (reads agent.yaml, handles extends)
│   │   ├── tool.py    # ToolLoader: .json → Tool (auto-wires built-ins; .sh for shell-backed tools)
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

## IPC — How Server and UI Communicate

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

## TODO

### LLM

- **`thinking` block support** — `AnthropicProvider` and `KimiForCodingProvider` both silently discard `thinking` blocks returned by models that support extended thinking (e.g. `kimi-for-coding`, Claude with extended thinking). The reasoning process is never surfaced in the UI or stored in history. Fix: detect `block.type == "thinking"` in `complete()` and forward via a dedicated callback or prepend to `on_text_chunk`.

---

## Changelog

### v0.9.2
- **`kimi_core` cleanup** — redundant placeholder prompts/ directory removed; entity now has only `agent.yaml` + empty `tools/` and `skills/` dirs.
- **`nutshell-new-agent` interactive picker** — running without `--extends`/`--standalone` now shows a numbered list of available entities to extend. Entity options are auto-detected from the entity directory. `-n NAME` flag is now optional (prompts interactively if omitted). Generated `agent.yaml` for inheriting entities is fully minimal (no redundant model/provider fields). `--no-inherit` kept as hidden backward-compat alias for `--standalone`.

### v0.9.1
- **Deep entity inheritance** — `AgentLoader` now supports arbitrarily deep `extends` chains (A→B→C). Parent is loaded recursively; null fields inherit the parent's already-resolved values rather than re-reading YAML. `resolve_file` walks the full ancestor directory chain for child-first file resolution. model/provider also inherit correctly from parent when not set.
- **`nutshell_dev` cleanup** — now extends `kimi_core` (3-level chain: nutshell_dev→kimi_core→agent_core). Redundant copies of prompts/ and tools/ removed; only the `nutshell` skill remains in the entity dir.

### v0.9.0
- **Tool provider layer** — `web_search` now has pluggable providers (Brave, Tavily). Set `tool_providers: {"web_search": "tavily"}` in `params.json` to switch. `nutshell/runtime/tool_provider_factory.py` mirrors the LLM `provider_factory.py` pattern; adding new providers requires only a one-line registry entry.
- **Tavily Search provider** — `nutshell/providers/tool/tavily.py`. Requires `TAVILY_API_KEY`. Same `web_search` tool schema and output format as Brave.
- **Shell-script-backed session tools** — agents can now create their own tools at session scope: write `sessions/<id>/tools/<name>.json` (schema) + `sessions/<id>/tools/<name>.sh` (implementation, receives JSON on stdin). Loaded fresh on every activation. `ToolLoader` detects `.sh` files automatically.

### v0.8.0
- **Skills redesign** — compliant with the [Agent Skills specification](https://agentskills.io/specification). Skills are now directories (`skills/<name>/SKILL.md`) instead of flat `.md` files. `Skill.prompt_injection` renamed to `Skill.body`; new `Skill.location` field (path to `SKILL.md`). File-backed skills use **progressive disclosure**: only name + description appear in a `<available_skills>` catalog in the system prompt; the model reads `SKILL.md` on demand via its bash/file tool. Inline skills (no `location`) retain the previous body-injection behavior for programmatic use.
- **`skill-creator` skill** — the [Anthropic skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator) is now bundled in `agent_core`, enabling agents to create and iterate on new skills.

### v0.7.0
- **Entity inheritance** — `extends: parent_name` in `agent.yaml`. Null field values signal "inherit from parent": prompts load from parent's directory, tools/skills inherit the parent's full list with per-file child-first fallback. `agent.yaml` always declares all fields for self-documentation.
- **`web_search` built-in tool** — Brave Search API (`BRAVE_API_KEY`). Added to `agent_core` and inherited by all child entities. Parameters: `query`, `count`, `country`, `language`, `freshness`, `date_after`, `date_before`.
- **`providers/` package** — `nutshell/llm/` removed; LLM providers live in `nutshell/providers/llm/`, search tools in `nutshell/providers/tool/`.
- **`nutshell-new-agent` revamp** — defaults to `--extends agent_core`, generating a minimal `agent.yaml` with empty override directories and placeholder files. `--no-inherit` retains old standalone behaviour.

### v0.6.1
- **Remove `read_tasks`/`write_tasks` injected tools** — task board is now managed directly via bash. `tasks.md` path is documented in the session context prompt. Removes two tool slots from every agent's context window.
- **Tasks panel UI** — last-updated timestamp and heartbeat interval moved from the panel header to bottom-right footer. Timestamp is derived from `tasks.md` file mtime instead of a status.json field.
- **`KimiForCodingProvider`** — Anthropic-compatible provider for Kimi For Coding (`https://api.kimi.com/coding/`). Thin wrapper over `AnthropicProvider` with custom base URL.
- **Anthropic SDK unification** — all providers use a single Anthropic SDK path.

### v0.6.0
- **`provider` field in `agent.yaml`** — entity manifests now declare a `provider` (`anthropic`, `kimi-coding-plan`). `AgentLoader` resolves and sets `agent._provider` on load.
- **`nutshell-new-agent` CLI** — scaffolds a new entity directory with `agent.yaml`, `prompts/system.md`, `prompts/heartbeat.md`, `skills/`, and `tools/bash.json`.
- **Clean startup init order** — `watcher.py` uses provider/model from `agent.yaml` as baseline; `params.json` acts as override only when explicitly set. Actual values always written back so `params.json` reflects reality.

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
