# Nutshell `v1.3.70`

A minimal Python agent runtime. Agents run as persistent server-managed sessions with autonomous heartbeat ticking. **Primary interface: CLI.**

New in v1.3.70: thinking_effort param in params.json (Codex: none/minimal/low/medium/high/xhigh; ignored by Anthropic/Kimi/OpenAI); agent entity defaults thinking=true; llm_engine/README.md thinking table updated.
New in v1.3.69: CodexProvider default model gpt-5.4 with high reasoning effort; thinking=True sends reasoning:{effort:"high"} per Codex Responses API; _supports_thinking=True; entity/README.md fixed to pass catalog tests.
New in v1.3.68: llm_engine audit — Kimi thinking enabled via extra_body (matches kimi-cli); Codex token refresh fixed to JSON body, reasoning_text.delta SSE event handled, misused include field removed; OpenAI/Codex _supports_thinking=False flags; cache breakpoint, tool fallback fixes; llm_engine/README.md fully documented.
New in v1.3.67: skill progressive disclosure now uses structured load_skill tool calls plus /skill slash-command expansion; skill catalog no longer exposes file paths.
New in v1.3.66: llm_engine refactor — AgentLoader moved to runtime/agent_loader.py; shared _common.py extracts _parse_json_args; openai_provider.py renamed to openai_api.py; llm_engine/README.md added.
New in v1.3.65: core/ pruned to the cleanest agent loop — dead ABCs, release_policy, as_tool, examples removed; hook.py adds OnLoopStart/OnLoopEnd/OnToolDone extension points; fallback_model/provider bug fixed.

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

## Changelog

### v1.3.67
- **Skill progressive disclosure via `load_skill`**: skill catalog now advertises only `name` + `description`, and agents are instructed to call `load_skill(name=...)` instead of manually reading `SKILL.md` files by path.
- **New built-in tool**: `nutshell/tool_engine/providers/load_skill.py`, registered with agent-context injection so skills can be loaded directly from the current agent's available skill set.
- **Slash command support**: `Session.chat()` now expands `/skill-name ...` into injected skill content plus trailing arguments, matching Claude Code-style skill activation.

### v1.3.65
- **core/ pruned to cleanest agent loop**: removed dead ABCs (`BaseTool`, `BaseAgent`), `release_policy`, `Agent.as_tool()`, `Agent._build_system_prompt()`, and `examples/`
- **`core/hook.py`**: new Hook type aliases — `OnLoopStart`, `OnLoopEnd`, `OnToolCall`, `OnToolDone`, `OnTextChunk`; `Agent.run()` gains three new extension points
- **Bug fix**: `session._load_session_capabilities()` now correctly applies `fallback_model` and `fallback_provider` from `params.json` to the agent
- **`philosophy.md`**: added project design principles document

### v1.3.63
- **WebSandbox**: added domain blocking and response truncation for `fetch_url` and `web_search`
- New session params: `blocked_domains` and `sandbox_max_web_chars`
- Session capability loader now injects web sandboxing into built-in fetch/search executors and provider overrides
- Added tests in `tests/tool_engine/test_web_sandbox.py`

### v1.3.42
- **Web UI rich tool call rendering** — `ui/web/index.html` now renders tool calls as structured cards instead of raw JSON.
- `bash` calls show a yellow tool badge header, timeout/pty badges, and a monospace command block with collapsible long commands (`▼ show more`).
- Added tailored summaries for `web_search`, `send_to_session`, `fetch_url`, and `propose_entity_update`, plus a concise fallback for other tools.

### v1.3.41
- **Meta-session layer via ordinary sessions** — entity-level mutable state now lives in `sessions/<entity>_meta/` instead of `entity/` or a separate top-level directory. Each meta-session uses the normal session layout (`core/memory.md`, `core/memory/`, `playground/`, optional `core/params.json`).
- **Meta session = entity instance** — child sessions are instantiated from the meta session, not directly from `entity/`: config is flattened into the meta session, while mutable memory and shared playground files are inherited from `sessions/<entity>_meta/`.
- **`session_factory.init_session()` seeding updated** — new sessions seed memory layers and playground files from `<entity>_meta`, with fallback to legacy `entity/<name>/memory.md` and `entity/<name>/memory/` for backward compatibility. Idempotency preserved.
- **New CLI: `nutshell meta [ENTITY]`** — inspect meta-session state and optionally print `core/memory.md`.
- Added tests for meta-session bootstrap and session seeding; full suite now passing (752 tests).


### v1.3.39
- **Agent Collaboration Mode** — two-part feature for multi-agent workflows:
  - **Caller detection**: `user_input` events carry `caller` field (`"human"` or `"agent"`). CLI uses `sys.stdin.isatty()`; `send_to_session` always writes `"agent"`. When caller is an agent, system prompt injects structured reply guidance (`[DONE]`/`[REVIEW]`/`[BLOCKED]`/`[ERROR]` prefixes).
  - **Git Master Node**: `GitCoordinator` class in `nutshell/runtime/git_coordinator.py` assigns master/sub roles per git remote URL. Registry at `_sessions/git_masters.json`. `git_checkpoint` output includes `[git:master]` or `[git:sub]` tag. Stale masters (dead PID) are auto-reclaimed. Session cleanup releases master claims.
- 35 new tests (`test_caller_detection.py`, `test_git_coordinator.py`); 732 total.
- **CAP (Cambridge Agent Protocol)**: `nutshell/runtime/cap.py` defines protocol primitives for supervised coordination (`handshake`, `lock`, `broadcast`, `heartbeat-sync`) and exposes `git_coordinator` as the first CAP protocol adapter.

### v1.3.38
- **TUI removed** — `ui/tui.py` deleted; `nutshell-tui` entry point and `textual` dependency removed from `pyproject.toml`; all references cleaned from `ui/cli/main.py` and `README.md`. Web UI is for humans, CLI is for agents.

### v1.3.37
- **Agent entity prompt improvements** (v1.1.0) — rewrote `entity/agent/` prompts based on context engineering and agent prompting best practices research
- `system.md`: added `<core_behaviors>` block with explicit directives (default-to-action, parallel tool use, step-by-step thinking, honesty), structured with XML tags, ~30% fewer tokens
- `heartbeat.md`: XML-wrapped task injection, priority focus directive, simplified paths, ~40% fewer tokens
- Added `entity/agent/CHANGELOG.md` for entity-level version tracking
- Research sources: Anthropic "Building Effective Agents", Anthropic prompting best practices (Claude 4.6), Karpathy/Lutke on context engineering


### v1.3.35
- **cli_os agent entity** — new `entity/cli_os/` implementing an immersive CLI-OS playground where the agent is root on a virtual Linux-like machine. Can freely explore, code, build projects, and experiment with any tool available in the shell
- Custom system prompt with workspace layout (`playground/{projects,tmp,output}/`), personality (curious, creative, hands-on), session continuity guidance, and rules of engagement
- New `cli-explorer` skill — comprehensive CLI exploration framework covering system discovery, project templates (Python, web server, data pipeline), experimentation patterns (try-and-learn, benchmarking, file processing), workspace management (cleanup, snapshots, git), advanced recipes (background processes, CLI tools, data exploration), and tips & tricks
- Tools scoped for exploration: bash, fetch_url, web_search, recall_memory, state_diff, app_notify (no dev tools like git_checkpoint or spawn_session)
- **`nutshell os`** command — launch or resume a CLI-OS playground session. Auto-continues the most recent cli_os session within 24 hours; `--new` forces a fresh session; accepts optional message argument
- On-demand entity (`persistent: false`, `heartbeat_interval: 600`, `max_iterations: 30`)
- 68 new tests across `test_entity_cli_os.py` (55) and `test_cli_os_cmd.py` (13); 672 total

### v1.3.34
- **yisebi agent entity** — new `entity/yisebi/` implementing an opinionated social media commentator ("懂王·行动派") who excels at analyzing trending topics, sharing unique perspectives, and crafting high-value comments across platforms
- Custom system prompt with 4-phase commentary approach (Scout → Analyze → Write → Engage), platform-specific tone guidance, comment philosophy, and domain strengths
- New `social-media` skill — comprehensive commentary framework covering topic discovery & source priority, angle-extraction lenses (incentive analysis, second-order effects, historical pattern matching, contrarian check, cross-domain connection), platform-specific writing guides (Twitter/X, Reddit, LinkedIn, 微博/小红书/知乎), engagement playbook, and meta-strategy for building a commentary voice
- Tools scoped for commentary: bash, web_search, fetch_url (no dev tools like git_checkpoint or spawn_session)
- On-demand entity (`persistent: false`, `heartbeat_interval: 600`)
- 57 new tests in `test_entity_yisebi.py`; 604 total

### v1.3.33
- **game_player agent entity** — new `entity/game_player/` implementing an elite gaming specialist that speedruns, high-scores, and optimally solves all types of games: text adventures, puzzles, strategy games, code challenges, riddles, and math games
- Custom system prompt with 4-phase game-playing approach (Recon → Strategize → Execute → Optimize), tool usage guidance, and game-type strategy table
- New `game-strategy` skill — comprehensive game classification taxonomy (information/players/determinism), universal solving framework (observe-analyze-decide-act-verify), strategy templates for maze/permutation/word-guessing/math/text-adventure/code-golf/riddles/strategy games, `state_diff` tracking patterns, and meta-strategies for when stuck
- Tools scoped for gameplay: bash, web_search, send_to_session, fetch_url, state_diff, recall_memory, app_notify (no dev tools like git_checkpoint or spawn_session)
- On-demand entity (`persistent: false`, `heartbeat_interval: 300`)
- 55 new tests in `test_entity_game_player.py`; 547 total

### v1.3.32
- **visit**: `nutshell visit [SESSION_ID]` — agent room view showing entity, status, recent activity (last 3 context entries), task board, and app notifications
- Supports `--json` for machine-readable output; defaults to latest session when no ID given

### v1.3.31
- **Auto-model selection** — new `auto_model` field in `params.json` (default: `false`). When enabled, `tick()` evaluates `tasks.md` complexity via lightweight text heuristics and temporarily overrides the agent model (haiku for simple, sonnet for medium, opus for complex). Supports Anthropic and OpenAI providers. Original model restored after each tick. Harness snapshot records `auto_model_override` field.
- New `nutshell/runtime/model_eval.py` — `evaluate_task_complexity()` + `suggest_model()` functions
- 23 new tests in `test_model_eval.py`; 470 total.


### v1.3.30
- **QjbQ — independent notification relay service** — new `qjbq/` top-level package (alongside `nutshell/`, `ui/`). FastAPI server on port 8081 with three endpoints: `POST /api/notify` (write app notification to any session), `GET /api/notify/{session_id}` (list notifications), `GET /health`. Lets agents send persistent, system-prompt-visible notifications to other sessions via HTTP.
- **`qjbq-server` CLI** — standalone entry point (`qjbq.cli:main`) to launch the relay server.
- **`nutshell-server --with-qjbq`** — optional flag to auto-start qjbq-server as a background process alongside the nutshell server.
- **`entity/agent/skills/qjbq/`** — new skill teaching agents how to use the QjbQ HTTP API (curl examples, endpoint reference).
- 13 new tests in `test_qjbq_server.py`; 447 total tests.

### v1.3.29
- **`nutshell chat --keep-alive`** — after receiving the reply, launches `nutshell-server` in the background so the session keeps its heartbeat active; prints `[heartbeat active — server running in background]`
- New `keep_alive` parameter on `_new_session()` in `ui/cli/chat.py`; wired through `cmd_chat` in `ui/cli/main.py`
- 11 new tests in `test_cli_keepalive.py`; 434 total

### v1.3.66
- **llm_engine refactor** — `AgentLoader` moved from `llm_engine/loader.py` to `nutshell/runtime/agent_loader.py`; `llm_engine/` now only owns provider implementations and registry
- `llm_engine/providers/_common.py` — shared `_parse_json_args()` extracted; used by `openai_api.py` and `codex.py`
- `openai_provider.py` renamed to `openai_api.py` for clarity
- `nutshell/llm_engine/README.md` added — full provider setup guide with env vars and Codex OAuth flow
- All import sites updated; 796 tests pass

### v1.3.28
- **OpenAI provider** — new `OpenAIProvider` in `nutshell/llm_engine/providers/openai_api.py`; supports GPT models via the official `openai` Python SDK
- Works with standard API keys and **openai-codex OAuth tokens** (`OPENAI_API_KEY` env var)
- Full feature parity: streaming text chunks, function calling (tools), token usage tracking (with cached-token support)
- Registered in `llm_engine/registry.py` as `"openai"` — switch via `params.json` (`provider: openai`, `model: gpt-5.4`)
- 20 new tests in `test_openai_api.py`; 423 total


### v1.3.27
- **persistent agent** — new `persistent` and `default_task` fields in `params.json`; when `persistent=true` and tasks are empty, tick() fires using `default_task` (or a built-in fallback) with `triggered_by='heartbeat_default'`
- Entity-level `params:` block in `agent.yaml` — propagated to session `params.json` on creation (supports `persistent`, `default_task`, `heartbeat_interval`)
- New `entity/persistent_agent/` — extends `agent` with 12-hour heartbeat, persistent mode, and a system prompt focused on autonomous state maintenance and message checking
- 13 new tests in `test_persistent_agent.py`; 403 total

### v1.3.26
- **receptionist agent entity** — new `entity/receptionist/` implementing the receptionist–worker pattern: a friendly front-desk agent handles user communication while delegating complex tasks to a background core agent
- Custom system prompt focused on communication, summarisation, and task delegation
- New `delegate` skill teaching spawn/monitor/collect lifecycle for worker agents
- Tools scoped to read-only bash + multi-agent coordination (no git_checkpoint, no propose_entity_update)
- 30 new tests in `test_entity_receptionist.py`; 390 total

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
- `params.json` also supports `blocked_domains` and `sandbox_max_web_chars` for `fetch_url` / `web_search` web sandboxing
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
- **Chat timeout default increased** — `nutshell chat` default `--timeout` raised from 120s to 300s. Complex agent tasks (especially with `--entity`) no longer time out prematurely while the agent is still working.

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
- **`propose_entity_update` tool + `nutshell review` CLI** — agents submit entity change requests for human review.

### v1.2.0
- **Anthropic prompt caching** — static prefix (system.md + session.md) cached; dynamic suffix (memory + skills) not cached.

### v1.1.9
- **`nutshell chat` CLI** — single-shot agent interaction. `send_to_session` system tool. `user_input_id` in turns for multi-agent polling.

### v1.1.7 — v1.1.8
- **Anthropic thinking block support**. **Layered session memory** (`core/memory/*.md`). **`reload_capabilities` summary**.

### v1.1.6
- **System prompt optimization** — `session.md` reduced to ~20 lines (table format).

### v1.0.0 — v1.1.5
- Dual-directory session layout. Entity inheritance (`extends`). Skills system. Provider layer (Anthropic, Kimi). Web UI. SSE streaming.


## Changelog

### v1.3.67
- **Skill progressive disclosure via `load_skill`**: skill catalog now advertises only `name` + `description`, and agents are instructed to call `load_skill(name=...)` instead of manually reading `SKILL.md` files by path.
- **New built-in tool**: `nutshell/tool_engine/providers/load_skill.py`, registered with agent-context injection so skills can be loaded directly from the current agent's available skill set.
- **Slash command support**: `Session.chat()` now expands `/skill-name ...` into injected skill content plus trailing arguments, matching Claude Code-style skill activation.

### v1.3.63
- **WebSandbox**: added domain blocking and response truncation for `fetch_url` and `web_search`
- New session params: `blocked_domains` and `sandbox_max_web_chars`
- Session capability loader now injects web sandboxing into built-in fetch/search executors and provider overrides
- Added tests in `tests/tool_engine/test_web_sandbox.py`

### v1.3.47
- **bridge layer** (`nutshell/runtime/bridge.py`): unified client-side session abstraction inspired by claude-code's replBridge patterns
- `BoundedIDSet`: FIFO ring buffer for event dedup — prevents echo and SSE reconnect re-delivery (O(1), O(capacity) memory)
- `BridgeSession`: wraps FileIPC with `send_message()`, `send_interrupt()`, `iter_events()` (sync+async), `wait_for_reply()` / `async_wait_for_reply()` — used by all frontends
- **soft interrupt**: `POST /api/sessions/{id}/interrupt` — drains pending input queue, defers next heartbeat tick, emits `{"type":"interrupted"}` event
- `ipc.py`: added `send_interrupt()` + `poll_interrupt(offset)` for daemon-side interrupt detection
- `session.py`: daemon loop checks interrupt each cycle before processing inputs
- `app.py`: SSE frames now carry `id: <seq>` header (Last-Event-ID standard reconnect); send_message uses BridgeSession; new interrupt endpoint
- `weixin.py`: replaced inline `_wait_for_agent_reply` with `BridgeSession.async_wait_for_reply(msg_id)` — uses `user_input_id` matching (no more false matches from concurrent heartbeat turns)

### v1.3.46
- **meta session as real agent**: `start_meta_agent()` creates `_sessions/<entity>_meta/` so watcher starts it as a persistent agent
- meta agent has built-in system + heartbeat prompts for dream cycle (24h interval, review all child sessions)
- `nutshell dream ENTITY` sends wake-up message to meta session (no more rule-based code)
- Removed `dream.py` — dream logic is now entirely agent-driven
- fix: `compute_meta_diffs` only flags diffs where entity has content (meta built-in prompts don't create false conflicts)
- fix: session memory dir only created when there are actual seed files (not just empty meta memory dir)
- fix: watcher indentation error in `_start_session`; removed `_maybe_auto_dream`

### v1.3.45
- **dream mechanism**: meta session periodically reviews all entity sessions, integrates memory, manages storage
- `nutshell/runtime/dream.py`: `run_dream()`, `DreamReport`, session classification (keep_active/keep_tracked/archive/delete)
- `nutshell dream [ENTITY] [--dry-run] [--force]` CLI command
- watcher auto-triggers dream when session count exceeds `dream_threshold` (default 30)
- agent.yaml fields: `dream_threshold`, `dream_interval`, `max_sessions`, `max_playground_mb`
- dream writes `sessions/<entity>_meta/core/memory/dream_sessions.md` + `dream_log.md`
- 27 new tests in `tests/runtime/test_dream.py`

### v1.3.44
- **gene feature**: `gene:` field in `agent.yaml` — list of shell commands executed once in meta session playground on first init
- meta session gets own `.venv` (isolated Python env with `--system-site-packages`)
- `nutshell meta ENTITY --init` to force re-run gene commands
- `_load_gene_commands()` walks `extends` chain to inherit gene from parent entities
- fix: `_resolve_entity_tools_dir()` walks extends chain so inherited entities get correct tools in meta session

### v1.3.43
- meta-session strict alignment with entity config
- new `alignment_blocked` session status and watcher-side blocking
- `nutshell meta --check/--sync` for resolving entity/meta drift

- **New built-in tool: `update_meta_memory`** — agents can persist cross-session mutable memory directly to their entity's meta-session without human approval.
- **QJBQ is now the canonical inter-session messaging path** — `send_to_session` relays via QJBQ `POST /api/session-message`, which writes inbound `user_input` events into the target `_sessions/<id>/context.jsonl`; direct file mutation remains only as a compatibility fallback when the relay is unavailable.

- **CAP (Cambridge Agent Protocol)**: `nutshell/runtime/cap.py` defines protocol primitives for supervised coordination (`handshake`, `lock`, `broadcast`, `heartbeat-sync`) and exposes `git_coordinator` as the first CAP protocol adapter.

- **Entity catalog curated**: `entity/README.md` now lists all maintained built-in entities, and each active entity has a local `README.md` describing purpose, status, and retention rationale.

- **Meta session as entity instance layer**: `sessions/<entity>_meta/` is the concrete runtime instantiation of an entity, seeding child sessions with flattened inherited config plus mutable memory and shared playground state.
