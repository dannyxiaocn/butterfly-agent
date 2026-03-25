# Nutshell `v1.3.5`

A minimal Python agent runtime. Agents run as persistent server-managed sessions with autonomous heartbeat ticking. **Primary interface: CLI.**

---

## Quick Start

```bash
pip install -e .
export ANTHROPIC_API_KEY=sk-...
export BRAVE_API_KEY=...       # optional: enables web_search (default provider)
export TAVILY_API_KEY=...      # optional: enables web_search via Tavily

nutshell server                # keep running in a terminal
nutshell chat "Plan a data pipeline"
# → prints agent response
# Session: 2026-03-25_10-00-00
```

---

## CLI

Single entry point for everything. Session management works **without a running server** — reads/writes `_sessions/` directly.

### Messaging

```bash
nutshell chat "Plan a data pipeline"                      # new session (entity: agent)
nutshell chat --entity kimi_agent "Review this code"      # custom entity
nutshell chat --session 2026-03-25_10-00-00 "Status?"     # continue session (server needed)
nutshell chat --session <id> --no-wait "Run overnight"    # fire-and-forget
nutshell chat --session <id> --timeout 60 "question"      # custom timeout (default: 120s)
```

### Session Management

```bash
nutshell sessions                     # list all sessions (table)
nutshell sessions --json              # JSON output — machine-readable for agents

nutshell new                          # create session (entity: agent, auto-generated ID)
nutshell new --entity kimi_agent      # specific entity
nutshell new my-project --entity agent  # specific ID

nutshell stop SESSION_ID              # pause heartbeat
nutshell start SESSION_ID             # resume heartbeat (server must be running)

nutshell tasks                        # show latest session's task board
nutshell tasks SESSION_ID             # show specific session's task board

nutshell log                          # show latest session's last 5 turns
nutshell log SESSION_ID               # specific session
nutshell log SESSION_ID -n 20         # last 20 turns
```

### Entity Management

```bash
nutshell entity new                           # interactive scaffold
nutshell entity new -n my-agent               # named, interactive parent picker
nutshell entity new -n my-agent --extends agent   # from specific parent
nutshell entity new -n my-agent --standalone  # standalone (no inheritance)
```

### Other

```bash
nutshell review                       # review pending agent entity-update requests
nutshell server                       # start the server daemon
nutshell web                          # start the web UI at http://localhost:8080 (monitoring)
```

---

## How It Works

```
nutshell server    ← always-on process: manages all sessions, dispatches heartbeats
nutshell web       ← optional web UI at http://localhost:8080 for monitoring
```

Everything is files. The server and UI communicate only through files on disk — no sockets, no shared memory. You can kill the UI, restart the server, and sessions resume exactly where they left off.

**Heartbeat loop:** agents work in cycles. Between activations they're dormant. The server fires a heartbeat on a configurable interval; the agent reads its task board, continues work, then goes dormant again. Non-empty task board = next wakeup fires. Empty board = all done.

---

## Filesystem as Everything

### Entity — Agent Definition

```
entity/<name>/
├── agent.yaml              ← name, model, provider, tools, skills, extends
├── prompts/
│   ├── system.md           ← agent identity and capabilities (concise)
│   ├── session.md          ← session file guide — {session_id} substituted at load time
│   └── heartbeat.md        ← injected into every heartbeat activation
├── skills/
│   └── <name>/SKILL.md     ← YAML frontmatter + body
└── tools/
    └── *.json              ← JSON Schema tool definitions
```

Entities can inherit from a parent with `extends: parent_name`. In `agent.yaml`, **null = inherit**, `[]` = explicitly empty, explicit list = override:

```yaml
name: my-agent
extends: agent
model: null          # inherit
prompts:
  system: null       # load from parent
  heartbeat: prompts/heartbeat.md  # own file
tools: null          # inherit parent's full list
skills: null         # inherit
```

```bash
nutshell entity new -n my-agent                    # extends agent (default)
nutshell entity new -n my-agent --extends kimi_agent
nutshell entity new -n my-agent --standalone
```

### Session — Live Runtime State

```
sessions/<id>/                ← agent-visible (reads/writes freely)
├── core/
│   ├── system.md             ← system prompt (copied from entity, editable)
│   ├── heartbeat.md          ← heartbeat prompt (editable)
│   ├── session.md            ← session file guide ({session_id} substituted at load)
│   ├── memory.md             ← persistent memory (auto-prepended to system prompt)
│   ├── memory/               ← layered memory: each *.md becomes "## Memory: {stem}"
│   ├── tasks.md              ← task board — non-empty triggers heartbeat
│   ├── params.json           ← runtime config: model, provider, heartbeat_interval
│   ├── tools/                ← agent-created tools: <name>.json + <name>.sh
│   └── skills/               ← agent-created skills: <name>/SKILL.md
├── docs/                     ← user-uploaded files (read-only for agent)
└── playground/               ← agent's workspace (tmp/, projects/, output/)

_sessions/<id>/               ← system-only (agent never sees this)
├── manifest.json             ← static: entity, created_at
├── status.json               ← dynamic: model_state, pid, status, last_run_at
├── context.jsonl             ← append-only conversation history
└── events.jsonl              ← runtime events: streaming, status, errors
```

**`core/params.json`** is read fresh before every activation:

```json
{
  "heartbeat_interval": 600.0,
  "model": null,
  "provider": null,
  "tool_providers": {"web_search": "brave"}
}
```

**bash default directory**: agents' bash commands run from `sessions/<id>/` — use short relative paths: `cat core/tasks.md`, `ls playground/`. Pass `workdir=...` to override per call.

---

## Defining an Agent

### `prompts/system.md`

Agent identity and capabilities — keep it concise. Operational details (file paths, task board usage, tool creation) belong in `session.md`, not here.

### `prompts/session.md`

Injected after `system.md` on every activation. `{session_id}` is substituted at load time.

### `prompts/heartbeat.md`

Injected on heartbeat activations. Minimal example:
```
Continue working on your tasks. When all tasks are done, respond with: SESSION_FINISHED
```

### Tools

**Two kinds, never mixed:**

| Kind | Who creates | How implemented | Hot-reload |
|------|------------|-----------------|-----------|
| **System tools** | Library only | Python (`tool_engine/`) | No |
| **Agent tools** | Agent at runtime | Shell script (`.json` + `.sh`) | Yes, via `reload_capabilities` |

**System tools (built-in, always available):**

| Tool | Purpose |
|------|---------|
| `bash` | Execute shell commands (runs from session dir by default) |
| `web_search` | Search via Brave or Tavily |
| `fetch_url` | Fetch a URL as plain text |
| `send_to_session` | Send a message to another session |
| `spawn_session` | Create a new sub-session |
| `recall_memory` | Search memory.md + memory/*.md |
| `propose_entity_update` | Submit a permanent improvement for human review |
| `reload_capabilities` | Hot-reload tools + skills from core/ |

**`web_search`**: default provider Brave (`BRAVE_API_KEY`). Switch to Tavily: `"tool_providers": {"web_search": "tavily"}` in `params.json`.

**Agent-created tools** (`.json` schema + `.sh` implementation). The script receives all kwargs as JSON on stdin, writes result to stdout:

```bash
#!/usr/bin/env bash
python3 -c "
import sys, json
args = json.load(sys.stdin)
print(args['query'].upper())
"
```

After writing both files, call `reload_capabilities`.

---

## Project Structure

```
nutshell/              ← Python library
├── core/
│   ├── agent.py       # Agent — LLM loop, system prompt assembly, tool dispatch
│   ├── tool.py        # Tool + @tool decorator
│   ├── skill.py       # Skill dataclass
│   ├── types.py       # Message, ToolCall, AgentResult, TokenUsage
│   ├── provider.py    # Provider ABC
│   └── loader.py      # BaseLoader ABC
├── tool_engine/
│   ├── executor/
│   │   ├── bash.py    # BashExecutor (subprocess + PTY)
│   │   └── shell.py   # ShellExecutor (JSON stdin→stdout for .sh tools)
│   ├── providers/web_search/  # brave.py, tavily.py
│   ├── registry.py    # get_builtin(), resolve_tool_impl()
│   ├── loader.py      # ToolLoader — .json → Tool; default_workdir support
│   └── reload.py      # create_reload_tool(session)
├── llm_engine/
│   ├── providers/
│   │   ├── anthropic.py   # AnthropicProvider (streaming, thinking, cache)
│   │   └── kimi.py        # KimiForCodingProvider
│   ├── registry.py
│   └── loader.py      # AgentLoader — entity/ dir → Agent (handles extends chain)
├── skill_engine/
│   ├── loader.py      # SkillLoader
│   └── renderer.py    # build_skills_block()
└── runtime/
    ├── session.py     # Session — reads files, runs daemon loop
    ├── ipc.py         # FileIPC — context.jsonl + events.jsonl
    ├── session_factory.py  # init_session() — shared session initialization
    ├── status.py      # status.json read/write
    ├── params.py      # params.json read/write
    ├── watcher.py     # SessionWatcher — polls _sessions/
    └── server.py      # nutshell-server entry point

ui/                    ← UI applications
├── cli/
│   ├── main.py        # nutshell — unified CLI entry point
│   ├── chat.py        # nutshell-chat (legacy alias)
│   ├── new_agent.py   # entity scaffolding
│   └── review_updates.py  # nutshell-review-updates
└── web/               # nutshell-web — monitoring UI (FastAPI + SSE)
    ├── app.py
    ├── sessions.py
    └── index.html
```

---

## IPC — How Server and Web UI Communicate

All IPC is file-based. Two append-only logs per session in `_sessions/<id>/`:

**`context.jsonl`** — conversation history:

| Event | Written by | Description |
|-------|-----------|-------------|
| `user_input` | UI / CLI | User message |
| `turn` | Server | Completed agent turn (messages + usage) |

**`events.jsonl`** — runtime signals:

| Event | Written by | Description |
|-------|-----------|-------------|
| `model_status` | Server | `{"state": "running|idle", "source": "user|heartbeat"}` |
| `partial_text` | Server | Streaming text chunk |
| `tool_call` | Server | Tool invocation before execution |
| `heartbeat_trigger` | Server | Before heartbeat run |
| `heartbeat_finished` | Server | Agent signalled `SESSION_FINISHED` |
| `status` | Server/CLI | Session status changes |
| `error` | Server | Runtime errors |

The web UI polls both files via SSE, resuming from the last byte offset on reconnect.

---

## Changelog

### v1.3.5
- **Entity `memory.md` seeding** — `session_factory.init_session()` now copies `entity/<name>/memory.md` into `core/memory.md` on first session creation (if absent). Entities can pre-seed agent memory.
- **`entity/nutshell_dev/memory.md`** — initial memory for nutshell_dev: project state, role definition, recent changes, SOP summary.
- **`entity/nutshell_dev/skills/nutshell/SKILL.md`** — updated to v1.3.4: new CLI commands, track.md workflow, role clarification.

### v1.3.4
- **`nutshell log [SESSION_ID] [-n N]`** — new CLI subcommand to display recent conversation history from a session's `context.jsonl`. Shows user messages, agent replies, tool calls, and token usage. Defaults to latest session, last 5 turns.
- 8 new tests in `test_cli_main.py` (27 total); 184 total tests.

### v1.3.3
- **`nutshell tasks [SESSION_ID]`** — new CLI subcommand to display a session's task board (`core/tasks.md`). Defaults to the most recently active session. Makes the agent's work visible to users from the terminal.
- 5 new tests in `test_cli_main.py`; 176 total.

### v1.3.2
- **TUI removed** — `ui/tui.py` deleted; `nutshell-tui` entry point removed. Web UI retained for monitoring.
- **README restructured** — CLI is the primary interface; web UI documented only as monitoring tool.
- Removed merged remote branches.

### v1.3.1
- **Unified `nutshell` CLI** — single entry point: `chat`, `sessions [--json]`, `new`, `stop`, `start`, `entity new`, `review`, `server`, `web`. Session management works without a running server.
- `ui/dui/new_agent.py` moved to `ui/cli/new_agent.py`.
- 14 new tests in `test_cli_main.py`; 168 total.

### v1.3.0
- **Bash/shell tools default to session directory** — agents use short relative paths (`cat core/tasks.md`). `ToolLoader(default_workdir=)` passed to `BashExecutor` + `ShellExecutor`.
- **TUI token usage** — `↑N ↓N · 📦N` footer after agent messages.
- 2 new tests; 154 total.

### v1.2.8
- **Web UI token usage display** — `↑N ↓N 📦N` footer on agent messages.
- 2 new tests in `test_ipc.py`; 152 total.

### v1.2.7
- **Token usage tracking** — `AgentResult.usage: TokenUsage` accumulated across tool loops. `AnthropicProvider` returns 3-tuple `(content, tool_calls, TokenUsage)`. Turn events in `context.jsonl` include `usage`.

### v1.2.6
- **Fix: all built-in tools in sessions** — 5 tools were missing from `entity/agent/agent.yaml` and never copied to `core/tools/`. All 7 tools now listed.

### v1.2.5
- **Heartbeat history pruning** — verbose heartbeat prompt replaced with compact `[Heartbeat <ts>]` marker after each tick. Prevents token accumulation over long sessions.

### v1.2.4
- **Conversation history caching** — Anthropic `cache_control: ephemeral` on last historical message. Multi-agent skill. Model-selection skill.

### v1.2.3
- **`fetch_url` + `recall_memory` tools** — fetch URLs as plain text; selective memory search without loading all memory into context.

### v1.2.2
- **`spawn_session` tool** — agents create sub-sessions dynamically. Shared `session_factory.init_session()`.

### v1.2.1
- **`propose_entity_update` tool + `nutshell-review-updates` CLI** — agents submit entity change requests for human review.

### v1.2.0
- **Anthropic prompt caching** — static prefix (system.md + session.md) cached; dynamic suffix (memory + skills) not cached.

### v1.1.9
- **`nutshell-chat` CLI** — single-shot agent interaction. `send_to_session` system tool. `user_input_id` in turns for multi-agent polling.

### v1.1.7 — v1.1.8
- **Anthropic thinking block support**. **Layered session memory** (`core/memory/*.md`). **`as_tool(clear_history=True)`**. **`reload_capabilities` summary**.

### v1.1.6
- **System prompt optimization** — `session.md` reduced to ~20 lines (table format).

### v1.0.0 — v1.1.5
- Dual-directory session layout. Entity inheritance (`extends`). Skills system. Provider layer (Anthropic, Kimi). Web UI. SSE streaming.
