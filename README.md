# Nutshell `v1.0.6`

A minimal Python agent runtime. Agents run as persistent server-managed sessions with autonomous heartbeat ticking, accessible via web browser.

---

## How It Works

```
nutshell-server    ← always-on process (manages all sessions)
nutshell-web       ← web UI at http://localhost:8080
nutshell-tui       ← terminal UI (Textual, no web server needed)
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
nutshell-web       # terminal 2a: web UI at http://localhost:8080
nutshell-tui       # terminal 2b: terminal UI (alternative to web)
```

To scaffold a new agent entity (inherits from `agent` by default):

```bash
nutshell-new-agent -n my-agent
```

---

## Filesystem as Everything

Nutshell's core design principle: **all state lives on disk**. Two kinds of directories — `entity/` (agent definitions) and `sessions/` (live runtime state).

### Entity — Agent Definition

```
entity/<name>/
├── agent.yaml              ← name, model, provider, tools, skills, extends
├── prompts/
│   ├── system.md           ← agent identity and rules
│   └── heartbeat.md        ← injected into every heartbeat prompt (optional)
├── skills/
│   └── <name>/SKILL.md     ← YAML frontmatter + body
└── tools/
    └── *.json              ← JSON Schema tool definitions
```

Entities can inherit from a parent with `extends: parent_name`. In `agent.yaml`, **null = inherit from parent**, `[]` = explicitly empty, an explicit list = override:

```yaml
name: my-agent
extends: agent
model: null          # inherit
provider: null       # inherit
prompts:
  system: null       # load from parent's directory
  heartbeat: prompts/heartbeat.md  # own file
tools: null          # inherit parent's full list
skills: null         # inherit parent's full list
```

Files missing in the child directory automatically fall back to the parent's copy.

```bash
nutshell-new-agent -n my-agent                    # extends agent (default)
nutshell-new-agent -n my-agent --extends kimi_agent
nutshell-new-agent -n my-agent --no-inherit       # standalone copy
```

### Session — Live Runtime State

Each session has two sibling directories:

```
sessions/<id>/                ← agent-visible (agent reads/writes freely)
├── core/
│   ├── system.md             ← system prompt (copied from entity, editable)
│   ├── heartbeat.md          ← heartbeat prompt (editable)
│   ├── session_context.md    ← session paths template
│   ├── memory.md             ← persistent memory (auto-appended to system prompt)
│   ├── tasks.md              ← task board
│   ├── params.json           ← runtime config: model, provider, heartbeat_interval, tool_providers
│   ├── tools/                ← agent-created tools: <name>.json + <name>.sh
│   └── skills/               ← skill directories
├── docs/                     ← user-uploaded files
└── playground/               ← agent's free workspace

_sessions/<id>/               ← system-only (agent never sees this)
├── manifest.json             ← static: entity, created_at (immutable)
├── status.json               ← dynamic: model_state, pid, status, last_run_at
├── context.jsonl             ← append-only conversation history
└── events.jsonl              ← runtime/UI events: streaming, status, errors
```

**`core/params.json`** is the source of truth for runtime config and is read fresh before every activation:

```json
{
  "heartbeat_interval": 600.0,
  "model": null,
  "provider": null,
  "tool_providers": {"web_search": "brave"}
}
```

Agents can modify their own configuration (model, provider, heartbeat interval, memory, skills, tools) by writing to `core/` — no server restart needed.

---

## Defining an Agent

### `prompts/system.md`

The agent's identity and rules. Include task board instructions if using heartbeat:

```markdown
You are a focused coding assistant.

## Task Board
Read and write `sessions/YOUR_ID/core/tasks.md` via bash.
Clear the file when all work is done.
```

### `prompts/heartbeat.md`

```markdown
Continue working on your tasks. When all tasks are done, respond with: SESSION_FINISHED
```

### Built-in Tools

**`bash`** — `command` (required), `timeout`, `workdir`, `pty` (PTY mode, Unix only)

**`web_search`** — `query` (required), `count` (1–10), `country`, `language`, `freshness` (day/week/month/year), `date_after`, `date_before` (YYYY-MM-DD). Default provider: Brave (`BRAVE_API_KEY`). Switch to Tavily by setting `tool_providers: {"web_search": "tavily"}` in `params.json`.

Both are auto-wired by name — just declare them in `agent.yaml`, no Python needed.

**Session-scoped custom tools** — agents create tools at runtime by writing `core/tools/<name>.json` (schema) + `core/tools/<name>.sh` (implementation, reads JSON from stdin).

---

## Project Structure

```
nutshell/              ← Python library package
├── core/
│   ├── agent.py       # Agent + BaseAgent — LLM loop, tool execution, history management
│   ├── tool.py        # Tool + BaseTool + @tool decorator
│   ├── skill.py       # Skill dataclass
│   └── types.py       # Message, ToolCall, AgentResult
├── providers/
│   ├── __init__.py    # Provider ABC
│   ├── llm/
│   │   ├── anthropic.py   # AnthropicProvider
│   │   └── kimi.py        # KimiForCodingProvider
│   └── tool/
│       ├── web_search.py  # Brave Search
│       └── tavily.py      # Tavily Search
└── runtime/
    ├── session.py          # Session — persistent context + heartbeat daemon loop
    ├── ipc.py              # FileIPC — context.jsonl + events.jsonl
    ├── status.py           # status.json read/write
    ├── params.py           # params.json read/write
    ├── provider_factory.py
    ├── tool_provider_factory.py
    ├── watcher.py          # SessionWatcher — polls _sessions/ directory
    ├── server.py           # nutshell-server entry point
    ├── loaders/
    │   ├── __init__.py     # BaseLoader ABC
    │   ├── agent.py        # AgentLoader: entity/ → Agent (handles extends chain)
    │   ├── tool.py         # ToolLoader: .json → Tool (.sh for shell-backed tools)
    │   └── skill.py        # SkillLoader: SKILL.md → Skill
    └── tools/
        ├── bash.py         # create_bash_tool(): subprocess + PTY
        └── _registry.py    # Built-in tool registry

ui/                    ← UI applications (separate from library)
├── web/                # nutshell-web (FastAPI + SSE)
│   ├── app.py          # routes + entry point
│   ├── sessions.py     # session helpers
│   └── index.html      # frontend (HTML + CSS + JS)
├── tui.py              # nutshell-tui (Textual terminal UI)
└── dui/                # developer UI — entity management CLI tools
    └── new_agent.py    # nutshell-new-agent
```

---

## IPC — How Server and UI Communicate

All IPC is file-based. Two append-only logs per session:

**`context.jsonl`** — pure conversation history:

| Event type | Written by | Description |
|-----------|-----------|-------------|
| `user_input` | UI | User message |
| `turn` | Server | Completed agent turn (full Anthropic-format messages) |

**`events.jsonl`** — runtime/UI signalling:

| Event type | Written by | Description |
|-----------|-----------|-------------|
| `model_status` | Server | `{"state": "running|idle", "source": "user|heartbeat"}` |
| `partial_text` | Server | Streaming text chunk |
| `tool_call` | Server | Tool invocation before execution |
| `heartbeat_trigger` | Server | Written before heartbeat run starts |
| `heartbeat_finished` | Server | Agent signalled `SESSION_FINISHED` |
| `status` | Server | Session status changes (resumed, cancelled) |
| `error` | Server | Runtime errors |

The web UI polls both files via SSE, resuming from the last byte offset on reconnect.

---

## TODO

- **`thinking` block support** — `AnthropicProvider` silently discards `thinking` blocks returned by models with extended thinking. Fix: detect `block.type == "thinking"` in `complete()` and forward via callback or prepend to `on_text_chunk`.

---

## Changelog

### v1.0.6
- **Package separation** — `ui/` moved from `nutshell/ui/` to repo root alongside `nutshell/`. UI is now a distinct application package (`ui.*`) that consumes the library (`nutshell.*`).

### v1.0.5
- **Package restructure** — removed `abstract/` module: `BaseAgent` inlined into `core/agent.py`, `BaseTool` into `core/tool.py`, `BaseLoader` into `runtime/loaders/__init__.py`, `Provider` into `providers/__init__.py`. No public API change.
- **DUI** — `cli/new_agent.py` moved to `ui/dui/new_agent.py` (developer UI, alongside web/tui frontends).

### v1.0.4
- **Terminal UI** — `nutshell-tui`: Textual-based three-panel TUI (sessions | chat | tasks). Reads files directly via `FileIPC` — only `nutshell-server` required, `nutshell-web` not needed. Features: session list with status indicators, full history replay, real-time polling (0.5s), streaming thinking indicator, task editor, stop/start/new session.

### v1.0.3
- **Web UI refactor** — `ui/web.py` (1000 lines) split into `ui/web/` package: `app.py`, `sessions.py`, `index.html`. Entry point `nutshell.ui.web:main` unchanged.
- **Code cleanup** — `_write_if_absent()` helper eliminates repeated pattern in `_init_session`; entity load failure now logs a warning.

### v1.0.2
- **Bug fixes** — history load `KeyError` on missing `content` key; `heartbeat_interval` clamped to ≥ 1.0 (prevents runaway firing); YAML frontmatter type guard in `SkillLoader`; invalid YAML in `nutshell_dev` SKILL.md; narrowed exception handling in `_load_session_capabilities`; watcher auto-expire errors now logged.

### v1.0.0 — v1.0.1
- **Session layout refactor** — dual-directory layout: `sessions/<id>/` (agent-visible, with `core/`, `docs/`, `playground/`) + `_sessions/<id>/` (system-only). Entity content copied to `core/` at session creation; entity dir not accessed at runtime.
- **Entity renames** — `agent_core` → `agent`, `kimi_core` → `kimi_agent`.
- **Default tool provider** — `DEFAULT_PARAMS` now sets `tool_providers: {"web_search": "brave"}` explicitly; `session_context.md` documents available providers.

### v0.9.x
- **Deep entity inheritance** — arbitrarily deep `extends` chains; child-first file resolution at every level.
- **`nutshell-new-agent` interactive picker** — numbered entity list, optional `-n NAME`, auto-detected options.
- **Tool provider layer** — pluggable `web_search` backend; `tool_provider_factory.py`; Tavily provider added.
- **Shell-script session tools** — agents create `core/tools/<name>.json` + `.sh` pairs at runtime.

### v0.7.x — v0.8.x
- **Entity inheritance** — `extends: parent_name` in `agent.yaml`; null fields inherit from parent.
- **Skills redesign** — directory layout (`skills/<name>/SKILL.md`), progressive disclosure, `skill-creator` bundled.
- **`web_search` built-in** — Brave Search API, added to base `agent` entity.
- **`providers/` package** — LLM + tool providers unified under `nutshell/providers/`.

### v0.5.x — v0.6.x
- **Streaming output** — `on_text_chunk` callback, real-time thinking bubble in web UI, markdown via `marked.js`.
- **Context/events split** — `context.jsonl` (history) + `events.jsonl` (runtime signals); SSE resumes from byte offset.
- **Provider field in `agent.yaml`** — entities declare their LLM provider; `KimiForCodingProvider` added.
- **Session capability reload** — `memory.md`, `skills/`, `params.json` all hot-reloaded per activation.
- **TUI removed** — web UI only.

### v0.1 — v0.4
- Initial server + web UI, persistent sessions, heartbeat, task board, `bash` built-in tool, `context.jsonl` IPC.
