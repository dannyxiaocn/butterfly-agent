# Nutshell `v1.3.70`

A minimal Python agent runtime. Agents run as persistent server-managed sessions with autonomous heartbeat ticking. **Primary interface: CLI.**

---

## Quick Start

```bash
pip install -e .
export ANTHROPIC_API_KEY=...
export KIMI_FOR_CODING_API_KEY=...  # optional: enables kimi_agent
export BRAVE_API_KEY=...            # optional: enables web_search (default provider)
export TAVILY_API_KEY=...           # optional: enables web_search via Tavily

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

nutshell visit                        # agent room view (latest session)
nutshell visit SESSION_ID             # specific session room view
nutshell visit --json                 # JSON output for agents

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

### Playground

```bash
nutshell os                           # launch / resume CLI-OS playground session
nutshell os 'build me a web server'   # open with a task
nutshell os --new                     # force a fresh session
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

``

### Meta-session — Entity-Level Mutable State

```
sessions/<entity>_meta/       ← ordinary session reserved as entity-level mutable state
├── core/memory.md            ← cross-session accumulated memory for that entity
├── core/memory/              ← layered cross-session memory
├── core/params.json          ← optional entity-level runtime params seed
└── playground/               ← shared workspace seed inherited by new sessions
```

`entity/` remains configuration-only. `sessions/<entity>_meta/` is the concrete instantiation unit for each entity: it seeds child sessions with inherited prompts/tools/skills plus mutable state from `core/memory.md`, `core/memory/`, and `playground/`. On first bootstrap, meta sessions copy memory and playground defaults from `entity/<name>/`; afterwards mutable cross-session state lives in the meta session. Use `propose_entity_update` only for durable entity changes that require review.

```

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
  "tool_providers": {"web_search": "brave"},
  "persistent": false,
  "default_task": null,
  "auto_model": false,
  "thinking": false,
  "thinking_budget": 8000,
  "thinking_effort": "high",
  "blocked_domains": [],
  "sandbox_max_web_chars": 50000
}
```

**bash default directory**: agents' bash commands run from `sessions/<id>/` — use short relative paths: `cat core/tasks.md`, `ls playground/`. Pass `workdir=...` to override per call.

### Auto-Model Selection

When `auto_model: true` in `params.json`, the system automatically evaluates task complexity before each heartbeat tick and selects an appropriate model:

| Complexity | Anthropic | OpenAI |
|------------|-----------|--------|
| simple | claude-haiku-4-5-20251001 | gpt-4o-mini |
| medium | claude-sonnet-4-6 | gpt-4o |
| complex | claude-opus-4-6 | o3 |

**Heuristics** (no LLM call — pure text analysis of `tasks.md`):
- **complex**: word count > 300, or contains keywords: implement, architect, design, refactor, migrate, debug, analyse/analyze, investigate, build
- **simple**: word count < 80, or contains keywords: check, list, status, ping, remind, note, log, summary
- **medium**: everything else

The override is temporary — the original model is restored after each tick. The harness snapshot records `auto_model_override` when active.

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

**Web sandbox**: set `blocked_domains` in `params.json` to deny `fetch_url` and `web_search` requests by hostname, and `sandbox_max_web_chars` to truncate large web responses.

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

### Using OpenAI Provider

Nutshell supports OpenAI GPT models (including via **openai-codex** OAuth tokens).

```bash
# Set your API key (or openai-codex OAuth token)
export OPENAI_API_KEY=<your_key_or_oauth_token>

# Optional: custom base URL
export OPENAI_BASE_URL=https://api.openai.com/v1
```

**Per-session** — set in `core/params.json`:

```json
{
  "provider": "openai",
  "model": "gpt-5.4"
}
```

**Per-entity** — set in `agent.yaml`:

```yaml
provider: openai
model: gpt-5.4
```

Features: streaming (`on_text_chunk`), function calling (tools), token usage tracking (including cached prompt tokens).

---

## Project Structure

```
nutshell/              ← Python library
├── core/
│   ├── agent.py       # Agent — the LLM loop
│   ├── hook.py        # Hook type aliases (OnLoopStart/End, OnToolCall/Done, OnTextChunk)
│   ├── tool.py        # Tool + @tool decorator
│   ├── skill.py       # Skill dataclass
│   ├── types.py       # Message, ToolCall, AgentResult, TokenUsage
│   ├── provider.py    # Provider ABC
│   └── loader.py      # AgentConfig — entity yaml reader
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
│   │   ├── openai_api.py  # OpenAIProvider (GPT / any OpenAI-compat endpoint)
│   │   ├── kimi.py        # KimiForCodingProvider (extends Anthropic)
│   │   ├── codex.py       # CodexProvider (ChatGPT OAuth, Responses API)
│   │   └── _common.py     # _parse_json_args() shared across providers
│   ├── registry.py
│   └── README.md      # provider setup guide (env vars, Codex OAuth flow)
├── skill_engine/
│   ├── loader.py      # SkillLoader
│   └── renderer.py    # build_skills_block()
└── runtime/
    ├── agent_loader.py  # AgentLoader — entity/ dir → Agent (extends chain, provider wiring)
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
│   ├── chat.py        # chat helpers used by `nutshell chat`
│   ├── new_agent.py   # entity scaffolding
│   └── review_updates.py  # review helpers used by `nutshell review`
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
---

## Agent Collaboration Mode

When one agent calls another (via `send_to_session` or piped CLI), the system automatically detects the caller type and adapts behaviour.

### Caller Detection

Every `user_input` event in `context.jsonl` carries a `caller` field:

| Source | `caller` value | How detected |
|--------|---------------|--------------|
| Interactive terminal | `"human"` | `sys.stdin.isatty()` in CLI |
| Piped/scripted CLI | `"agent"` | `sys.stdin.isatty()` returns False |
| `send_to_session` tool | `"agent"` | Always — agent-to-agent messaging |

When `caller` is `"agent"`, the system prompt is extended with **structured reply guidance** requiring the agent to prefix its final reply with one of:

- **`[DONE]`** — task completed successfully
- **`[REVIEW]`** — work finished but needs human review
- **`[BLOCKED]`** — cannot proceed; explains what is needed
- **`[ERROR]`** — unrecoverable error with diagnostics

This makes agent replies machine-parseable for the calling agent.

### Git Master Node

When multiple agent sessions work on the same git repository, a **master/sub** coordination protocol prevents conflicts:

- **Registry**: `_sessions/git_masters.json` maps each git remote URL to a master session
- **First session wins**: the first session to `git_checkpoint` a repo becomes master
- **Stale reclamation**: if the master session's PID is no longer running, a new session can claim master
- **Auto-release**: when a session daemon stops, it releases all master claims

`git_checkpoint` output now includes a role tag: `Committed abc1234: message [git:master]` or `[git:sub]`.


## Agent's Perspective for Improving Nutshell

> This section is maintained by nutshell_dev and other agents running inside Nutshell.
> Anything surprising, frustrating, or worth improving gets recorded here.

### Observability & Debugging
- **No tool result visibility in web UI**: tool calls are now rendered nicely (v1.3.42), but tool *results* (what the tool returned) are never shown. When a bash command errors or returns unexpected output, there's no way to see it in the web UI without checking `context.jsonl` manually.
- **Token report only accessible via CLI**: `nutshell token-report` is useful but not exposed in the web UI. A small token cost summary per session in the sidebar would help spot runaway sessions early.
- **No way to view `core/memory.md` from the web UI**: agents update memory but there's no in-browser way to inspect it. Reading it requires SSH/terminal.

### Agent Experience
- **`send_to_session` timeout is silent on expiry**: when the target session doesn't respond within `timeout` seconds, the tool returns an error string but gives no indication of whether the message was received. A "delivered but no reply yet" vs "not delivered" distinction would help.
- **No streaming tool results**: tool calls appear immediately (streaming), but there's no way to stream *partial output* from a long-running bash command. The agent can't see incremental output either — it only gets the full result when the command finishes.
- **Memory layer truncation is invisible to the agent**: when `memory_layers` >60 lines are truncated in the system prompt, the agent only gets a bash hint. It's easy to forget that a layer was truncated and act on stale context.

### System Reliability
- **Playground push fails to non-bare remotes**: nutshell_dev always hits `receive.denyCurrentBranch` when pushing back to the origin repo from the playground clone. Requires the orchestrating Claude Code to `git fetch` + merge manually. Either making the origin bare or using a different push strategy would eliminate this friction.
- **Session venv creation can be slow on first start**: `_create_session_venv()` runs `python -m venv --system-site-packages` synchronously during session init, which blocks the startup. Could be deferred or run in a background thread.

### Missing Capabilities
- **No way to cancel an in-flight tool call**: if a bash command runs for too long, the agent has no mechanism to interrupt it mid-flight. The session can be stopped, but that kills everything.
- **No structured output / typed tool responses**: tools return free-form strings. A structured result format (e.g. `{ok: bool, output: str, error?: str}`) would let agents reason more reliably about success vs failure.
- **No inter-session shared filesystem namespace**: sessions can communicate via `send_to_session`, but can't easily share files. A `shared/` directory visible to all sessions of the same entity would be useful for passing large artifacts without copying through messages.

---
