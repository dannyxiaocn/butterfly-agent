# Nutshell `v1.3.25`

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
nutshell chat --session <id> --timeout 60 "question"      # custom timeout (default: 300s)
nutshell chat --inject-memory key=value "message"         # inject memory layer before first turn
nutshell chat --inject-memory track=@track.md "start"     # inject file contents as memory layer
```

### Session Management

```bash
nutshell sessions                     # list all sessions (table)
nutshell sessions --json              # JSON output — machine-readable for agents

nutshell friends                      # IM-style contact list with status dots
nutshell friends --json               # JSON output for agents

nutshell kanban                       # unified task board (all sessions)
nutshell kanban --session ID          # single session
nutshell kanban --json                # JSON output for agents

nutshell new                          # create session (entity: agent, auto-generated ID)
nutshell new --entity kimi_agent      # specific entity
nutshell new my-project --entity agent  # specific ID
nutshell new --inject-memory key=value  # inject memory layer on creation

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
nutshell tui                          # start the terminal UI (sessions · chat · tasks)
```

---

## How It Works

```
nutshell server    ← always-on process: manages all sessions, dispatches heartbeats
nutshell web       ← optional web UI at http://localhost:8080 for monitoring
nutshell tui       ← optional terminal UI: sessions list, live chat, tasks panel
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

### Memory

Each session has two memory structures, both re-read from disk on **every activation**:

| File | Prompt block | Writable by agent |
|------|-------------|-------------------|
| `core/memory.md` | `## Session Memory` | Yes — `echo/cat >` via bash |
| `core/memory/<name>.md` | `## Memory: <name>` | Yes — write any `.md` file |

**Memory is injected after `session.md` but before skills**, so it's in the dynamic (non-cached) suffix.

**Agents update session memory by writing files:**
```bash
# Overwrite primary memory
echo "Last task: feature X done (commit abc123)" > core/memory.md

# Add/update a named layer
cat > core/memory/work_state.md << 'EOF'
## Current Task
Implementing feature Y
EOF
```

Changes take effect on the **next activation** — the runtime re-reads from disk each time.

**Cross-session memory** (for entities like `nutshell_dev`): update the entity's template files in `entity/<name>/memory.md` + `entity/<name>/memory/*.md` and push. `session_factory` seeds new sessions from these templates.

### nutshell_dev — autonomous development agent

`nutshell_dev` is an entity that develops nutshell itself. Two usage modes:

**Dispatched mode** (Claude Code → nutshell_dev):
```bash
nutshell chat --entity nutshell_dev --timeout 300 "任务：<description>"
```

**Autonomous heartbeat mode** (self-selects tasks from track.md):
```bash
# Create a persistent session
nutshell new --entity nutshell_dev dev-session

# Start the server (picks up the session and runs heartbeats)
nutshell server
```

On each heartbeat, `nutshell_dev` reads `track.md`, picks the first actionable `[ ]` task, implements it following its SOP (clone → implement → test → commit → mark done → push), then picks the next task. Stops when no actionable items remain.

```
Session memory:   sessions/<id>/core/memory.md        ← per-session, mutable
                  sessions/<id>/core/memory/<name>.md  ← named layers, mutable
Entity template:  entity/<name>/memory.md              ← seeds new sessions
                  entity/<name>/memory/<name>.md        ← seeds named layers
```

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

### v1.3.25
- **kanban**: `nutshell kanban` — unified task-board view across all sessions; shows entity, online/idle/offline status, and `tasks.md` content per session
- `--session ID` to filter a single session; `--json` for machine-readable output
- 16 new tests in `test_cli_kanban.py`; 360 total

### v1.3.24
- **app notifications**: new `core/apps/` directory — `.md` files are injected as an **App Notifications** block in the system prompt on every activation, giving agents a persistent, always-visible channel for status updates, alerts, and cross-app communication
- **`app_notify` tool**: built-in tool with `write` / `clear` / `list` actions to manage `core/apps/<app>.md` files; registered in `entity/agent/agent.yaml`
- Agent `_build_system_parts()` renders app notifications between memory and skills in the dynamic suffix
- Session `_load_session_capabilities()` reads `core/apps/*.md` (sorted, non-empty only)
- `entity/agent/prompts/session.md` documents the `core/apps/` directory and usage
- 17 new tests in `test_app_notify.py`; 344 total

### v1.3.23
- **fix: partial_text flush** — `_make_text_chunk_callback()` now exposes a `.flush()` method; `chat()` and `tick()` call it in a `finally` block so the last <150-char buffered segment is always emitted as a `partial_text` event (previously silently dropped)
- 7 new tests in `test_text_chunk_flush.py`; 327 total

### v1.3.22
- **friends**: `nutshell friends` — IM-style session list with online/idle/offline status indicators (●/◐/○)
- Status engine: `model_state=running` or last_run <5m → online, <1h → idle, else offline; `stopped` always offline
- `--json` flag for machine-readable output (agents can parse peer list programmatically)
- **messaging skill**: new `entity/agent/skills/messaging/SKILL.md` teaches agents to discover peers via `nutshell friends` and communicate via `send_to_session`
- 9 new tests in `test_friends.py`; 320 total

### v1.3.21
- **repo-dev**: `nutshell repo-dev <path>` creates a dedicated dev-agent session for any repo
- Generates a codebase-overview skill (`<name>-wiki`) and injects it into a new `nutshell_dev` session
- `--name/-n` overrides project name, `--message/-m` sends an initial task to the agent
- Session ID format: `repo-dev-<name>-<timestamp>` for easy identification
- 6 new tests in `test_repo_dev.py`

### v1.3.20
- **repo-skill**: `nutshell repo-skill <path>` generates a `SKILL.md` codebase overview from any repo
- Pure filesystem ops (no LLM) — extracts README summary, directory tree, key files
- Auto-detects manifests (`pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`…), entry points, source dirs
- `--output DIR` and `--name NAME` options; defaults to `core/skills/<name>-wiki/` inside current session
- 17 new tests in `test_repo_skill.py`

### v1.3.19
- **Sandbox**: bash executor now checks commands against `DANGEROUS_DEFAULTS` before execution
- Blocks destructive ops (`rm -rf /`, `mkfs`, `dd of=/dev/`), system cmds (`shutdown`, `reboot`), fork bombs, credential access
- `params.json` supports `blocked_patterns` list for session-level custom regex blocking
- `ToolLoader` passes `blocked_patterns` through to `BashExecutor` automatically
- 24 new tests in `test_sandbox.py`

### v1.3.18
- **Harness feedback system**: After every agent turn, a performance snapshot is automatically written to `core/memory/harness.md`. The agent sees it next turn as a memory layer: triggered_by, iterations, tool_calls, tokens (input/output/cache), history_turns, model. Enables self-adjustment without external monitoring. Works for both `chat()` (user-triggered) and `tick()` (heartbeat) turns.
- `AgentResult.iterations` — new field counting tool-call loop iterations per turn, set in `agent.run()`.
- 11 new tests in `test_harness.py`; 264 total.

### v1.3.17
- **`nutshell token-report [SESSION_ID]`**: New diagnostic command showing per-turn token costs — columns: turn #, timestamp, trigger preview, input/output/cache-read/cache-write tokens. Includes session totals, cache hit rate, and top-3 most expensive turns. Makes prompt token economics visible so users and agents can find cheaper paths.
- 10 new tests in `test_cli_token_report.py`; 253 total.

### v1.3.16
- **`git_checkpoint` built-in tool**: Agents can now call `git_checkpoint(message, workdir)` to stage all changes and create a checkpoint commit in a git repository within their session workspace. Returns the commit hash + summary, or `(nothing to commit)` if the tree is clean. Designed for the nutshell_dev playground workflow: `git_checkpoint(message="feat: X", workdir="playground/nutshell")`.
- `nutshell/tool_engine/providers/git_checkpoint.py` — implementation.
- `entity/agent/tools/git_checkpoint.json` — schema; added to `entity/agent/agent.yaml`.
- 9 new tests in `test_git_checkpoint.py`; 243 total.

### v1.3.15
- **`nutshell prompt-stats [SESSION_ID]`**: New diagnostic command showing a component-by-component breakdown of system prompt size — static (cached: `system.md`, `session.md`), dynamic (`memory.md`, memory layers with truncation notes, skills catalog), and heartbeat sections. Columns: Lines (disk), Chars (prompt), ~Tokens (chars/4). Helps reason about prompt space allocation and cost vs. effectiveness trade-offs.
- 9 new tests in `test_cli_prompt_stats.py`; 234 total.

### v1.3.14
- **Entity version control**: Every entity now has a `version` field in `agent.yaml` (starting at `1.0.0`). When a human applies an agent-proposed update via `nutshell review`, the patch version is bumped automatically (`1.0.0 → 1.0.1`) and a changelog entry is appended to `entity/<name>/CHANGELOG.md`, recording the file changed, session ID, and reason.
- `nutshell entity log <name>` — new subcommand to display an entity's version and full changelog.
- `nutshell/runtime/entity_updates.py`: `bump_entity_version()`, `get_entity_version()`, `get_entity_changelog()`, `_extract_entity_name()`, `_bump_patch()`.
- All three built-in entities (`agent`, `kimi_agent`, `nutshell_dev`) seeded with `version: 1.0.0`.
- 10 new tests in `test_entity_update.py`; 225 total.

### v1.3.13
- **`state_diff` built-in tool**: Token-efficient state tracking for high-frequency status checks. `state_diff(key, content)` stores a named snapshot in `core/state/<key>.txt` and returns a unified diff on subsequent calls. Returns "(initialized)" on first call, "(no change)" when unchanged. Designed for use with `ps`, `df`, `git status`, etc. to avoid re-reading 50+ identical lines every heartbeat.
- `nutshell/tool_engine/providers/state_diff.py` — implementation.
- `entity/agent/tools/state_diff.json` — schema; added to `entity/agent/agent.yaml`.
- 8 new tests in `test_state_diff.py`; 215 total.

### v1.3.12
- **TUI restored**: `nutshell tui` launches a Textual terminal UI with a three-pane layout: session list (left), live chat log with rich markdown (center), and task board editor (right). Real-time polling (0.5s events, 3s sessions, 2s tasks). Supports send message, stop/resume session, create new session, edit tasks.
- `ui/tui.py` re-introduced; `nutshell-tui` entry point added to `pyproject.toml`.
- `nutshell tui` subcommand added to the unified CLI.
- Refactored vs old TUI: uses shared `_read_session_info`/`_sort_sessions` from `ui.web.sessions` instead of duplicating session-reading logic.

### v1.3.11
- **nutshell_dev autonomous heartbeat**: `entity/nutshell_dev` now ships a custom `prompts/heartbeat.md` that drives fully autonomous task selection from `track.md`. On each heartbeat: empty task board → reads `track.md`, picks the first actionable `[ ]` item, writes it to `core/tasks.md`, and begins; non-empty board → follows SOP, commits, pushes, marks done, then picks the next task or returns `SESSION_FINISHED`.
- `entity/nutshell_dev/agent.yaml` updated: `heartbeat: prompts/heartbeat.md`.
- README documents autonomous mode (`nutshell new --entity nutshell_dev` + `nutshell server`).

### v1.3.10
- **Memory layer progressive disclosure**: Large named memory layers (`core/memory/*.md`) are now truncated in the system prompt using the same approach as file-backed skills. Layers within 60 lines are injected verbatim; larger layers show the first 60 lines plus a bash hint (`cat core/memory/<name>.md`) so the agent reads the rest on demand. Primary `memory.md` is unaffected.
- `Agent._MEMORY_LAYER_INLINE_LINES = 60` — tunable class-level threshold.
- `Agent._render_memory_layer(name, content)` — new classmethod for rendering.
- 5 new tests in `test_session_capabilities.py`; 207 total.

### v1.3.9
- **CLI/web parity — no more competing daemons**: `nutshell server` (watcher) now checks `pid_alive` before starting a session daemon. If a daemon is already running (e.g. started by `nutshell chat`), the watcher skips that session. Once the CLI daemon exits, the watcher picks it up on the next scan — seamless handoff.
- `pid_alive()` moved from `ui/web/sessions.py` to `nutshell/runtime/status.py` for shared use by watcher and web.
- 7 new tests in `test_watcher.py`; 202 total.

### v1.3.8
- **`--inject-memory KEY=VALUE / KEY=@FILE`** — `nutshell chat` and `nutshell new` accept one or more `--inject-memory` flags that write named memory layers (`core/memory/<KEY>.md`) before the first agent turn. Supports inline values (`key=hello`) and file references (`track=@track.md`). Enables dynamic context injection (e.g. live task list) when spawning agents from scripts.
- 8 new tests in `test_cli_main.py`; 195 total.

### v1.3.7
- **Chat timeout default increased** — `nutshell chat` and `nutshell-chat` default `--timeout` raised from 120s to 300s. Complex agent tasks (especially with `--entity`) no longer time out prematurely while the agent is still working.

### v1.3.6
- **Entity layered memory seeding** — `session_factory.init_session()` now copies all `.md` files from `entity/<name>/memory/` into `session/core/memory/` on first creation (idempotent). Entities can pre-seed layered memory layers alongside the flat `memory.md`.
- **`entity/nutshell_dev/memory/track_sop.md`** — layer memory teaching nutshell_dev how to read/complete/update track.md tasks.
- 3 new tests in `test_spawn_session.py`; 187 total.

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
