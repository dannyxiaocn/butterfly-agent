# Nutshell `v1.2.7`

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

## CLI

```bash
# New session (no server required — spins up a daemon for one response)
nutshell-chat "Plan a data pipeline"
# → prints agent response, then:
# Session: 2026-03-24_10-00-00

# Continue an existing session (requires nutshell-server running)
nutshell-chat --session 2026-03-24_10-00-00 "Status update?"

# Different entity
nutshell-chat --entity kimi_agent "Review this code snippet"

# Fire-and-forget (don't wait for response)
nutshell-chat --session <id> --no-wait "Run overnight report"

# Custom timeout
nutshell-chat --session <id> --timeout 60 "Complex question"
```

---

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
│   ├── system.md           ← agent identity and capabilities (concise)
│   ├── session.md          ← session file guide — injected with real {session_id} each run
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
│   ├── session.md            ← session file guide (session_id substituted at load time)
│   ├── memory.md             ← persistent memory (auto-appended to system prompt)
│   ├── tasks.md              ← task board
│   ├── params.json           ← runtime config: model, provider, heartbeat_interval, tool_providers
│   ├── tools/                ← agent-created tools: <name>.json + <name>.sh
│   └── skills/               ← skill directories
├── docs/                     ← user-uploaded files
└── playground/               ← agent's computer (tmp/, projects/, output/)

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

The agent's identity and capabilities — keep it concise. Session-specific operational details (file paths, task board usage, tool/skill creation) belong in `session.md`, not here.

### `prompts/session.md`

A template injected after `system.md` on every activation. The string `{session_id}` is substituted with the real session ID at load time. Use this to give the agent accurate, clickable paths to all its session files (`core/tasks.md`, `core/memory.md`, `core/tools/`, etc.).

Old sessions with `session_context.md` continue to work — the runtime falls back automatically.

### `prompts/heartbeat.md`

```markdown
Continue working on your tasks. When all tasks are done, respond with: SESSION_FINISHED
```

### Tools

**Tool taxonomy — two kinds, never mixed:**

| Kind | Who creates | How implemented | Hot-reload |
|------|------------|-----------------|-----------|
| **System tools** | Library only | Python (in `tool_engine/`) | No |
| **Agent tools** | Agent at runtime | Shell script (`.json` + `.sh`) | Yes, via `reload_capabilities` |

**System tools (built-in):**

**`bash`** — `command` (required), `timeout`, `workdir`, `pty` (PTY mode, Unix only)

**`web_search`** — `query` (required), `count` (1–10), `country`, `language`, `freshness` (day/week/month/year), `date_after`, `date_before` (YYYY-MM-DD). Default provider: Brave (`BRAVE_API_KEY`). Switch to Tavily by setting `tool_providers: {"web_search": "tavily"}` in `params.json`.

Both are Python-implemented, system-only, declared in `entity/agent/tools/`.

**`reload_capabilities`** — always injected by the session, cannot be overridden from disk. Triggers hot-reload of all tools, skills, and prompts from `core/` mid-session.

**Agent-created tools (shell-backed):**

Agents create tools at runtime by writing `core/tools/<name>.json` (schema) + `core/tools/<name>.sh` (implementation). The `.sh` script receives all kwargs as a JSON object on stdin and writes its result to stdout. Agents may use Python (or any interpreter) inside the script — it is still a shell tool from the system's perspective.

```bash
# Example .sh tool
#!/bin/bash
python3 -c "
import sys, json
args = json.load(sys.stdin)
print(args['query'].upper())
"
```

After writing both files, call `reload_capabilities` to make the tool available immediately without restarting.

---

## Project Structure

```
nutshell/              ← Python library package
├── core/
│   ├── agent.py       # Agent + BaseAgent — LLM loop, system prompt assembly, tool dispatch
│   ├── tool.py        # Tool + BaseTool + @tool decorator
│   ├── skill.py       # Skill dataclass
│   ├── types.py       # Message, ToolCall, AgentResult
│   ├── provider.py    # Provider ABC
│   └── loader.py      # BaseLoader ABC
│
├── tool_engine/       ← All tool processing (loading, execution, provider swap)
│   ├── executor/
│   │   ├── base.py    # BaseExecutor ABC
│   │   ├── bash.py    # BashExecutor + create_bash_tool() — subprocess + PTY
│   │   └── shell.py   # ShellExecutor — JSON stdin→stdout for .sh agent tools
│   ├── providers/
│   │   └── web_search/
│   │       ├── brave.py    # _brave_search() (BRAVE_API_KEY)
│   │       └── tavily.py   # _tavily_search() (TAVILY_API_KEY)
│   ├── registry.py    # get_builtin(), resolve_tool_impl(), list_providers()
│   ├── loader.py      # ToolLoader — .json → Tool via executor chain
│   ├── reload.py      # create_reload_tool(session) — hot-reload built-in
│   └── sandbox.py     # PLACEHOLDER
│
├── llm_engine/        ← Self-contained LLM provider layer
│   ├── providers/
│   │   ├── anthropic.py   # AnthropicProvider
│   │   └── kimi.py        # KimiForCodingProvider
│   ├── registry.py    # resolve_provider(), provider_name()
│   └── loader.py      # AgentLoader — entity/ dir → Agent (handles extends chain)
│
├── skill_engine/      ← All skill processing (loading, rendering)
│   ├── loader.py      # SkillLoader — SKILL.md → Skill
│   └── renderer.py    # build_skills_block() — renders skills into system prompt
│
└── runtime/           ← Pure orchestration (no business logic)
    ├── session.py     # Session — reads files, assigns agent fields, runs daemon loop
    ├── ipc.py         # FileIPC — context.jsonl + events.jsonl
    ├── status.py      # status.json read/write
    ├── params.py      # params.json read/write
    ├── watcher.py     # SessionWatcher — polls _sessions/ directory
    └── server.py      # nutshell-server entry point

ui/                    ← UI applications (separate from library)
├── web/               # nutshell-web (FastAPI + SSE)
│   ├── app.py         # routes + entry point
│   ├── sessions.py    # session helpers
│   └── index.html     # frontend (HTML + CSS + JS)
├── tui.py             # nutshell-tui (Textual terminal UI)
└── dui/               # developer UI — entity management CLI tools
    └── new_agent.py   # nutshell-new-agent
```

**Layer boundaries:**

| Layer | Owns |
|-------|------|
| `core/` | Data types (Skill, Tool, Message, …) + Agent loop + system prompt assembly |
| `tool_engine/` | Tool loading, executor dispatch, provider swap, hot-reload |
| `llm_engine/` | LLM provider implementations + AgentLoader |
| `skill_engine/` | Skill loading + skill→prompt rendering |
| `runtime/` | File I/O → assign agent fields → trigger runs. No string formatting. |

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

---

## Changelog

### v1.2.7
- **Token usage tracking** — `AgentResult` now includes a `TokenUsage` field with `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`. Usage is accumulated across all tool-call iterations within a single `agent.run()`. `Session.chat()` and `Session.tick()` write `usage` field to turn events in `context.jsonl` when tokens > 0.
- `AnthropicProvider.complete()` now returns `(content, tool_calls, TokenUsage)` — third value contains Anthropic token counts including cache hit/miss stats.
- `Provider` ABC updated to return 3-tuple; `KimiProvider` inherits correctly.
- 4 new tests; 150 total.

### v1.2.6
- **Fix: all built-in tools now available in sessions** — `entity/agent/agent.yaml` previously only listed `bash` and `web_search`. The other 5 built-in tools (`send_to_session`, `spawn_session`, `propose_entity_update`, `fetch_url`, `recall_memory`) were present as `.json` files but omitted from agent.yaml, so they were never copied to `core/tools/` during session init. Sessions had no access to these tools. All tools now listed in agent.yaml.
- 1 new test in `test_agent_loader_inheritance.py` (asserts all 7 built-in tools present); 146 total.

### v1.2.5
- **Heartbeat history pruning** — after each heartbeat activation, the verbose heartbeat prompt in agent history is replaced with a compact `[Heartbeat <ts>]` marker. For sessions with many heartbeat cycles, this prevents heartbeat instructions from accumulating in the context window, significantly reducing token costs on long-running tasks.
- `_reshape_history()` updated to also recognize the compact marker format (alongside the old "Heartbeat activation" prefix).
- 2 new tests in `test_session_capabilities.py`; 145 total.

### v1.2.4
- **Conversation history caching** — `AnthropicProvider` now adds `cache_control: ephemeral` to the last historical message in each API call. For long sessions (many turns), all prior conversation is served from cache on subsequent activations, significantly reducing token costs.
- **`cache_last_human_turn` param** — added to `Provider.complete()` ABC and `AnthropicProvider`/`KimiProvider`. Kimi correctly ignores cache_control (no-op).
- **`multi-agent` skill** — documents `spawn_session` + `send_to_session` coordination patterns: delegate-and-wait, fire-and-forget, worker pool, coordinator/receptionist.
- **`model-selection` skill** — teaches agents when to switch to Haiku (fast/cheap) vs Opus (complex reasoning) via `params.json`, with decision guide and provider switching.
- **`nutshell_dev` skill updated** — reflects v1.2.x package layout, correct paths, 143 tests, all new built-in tools.
- 8 new tests in `test_prompt_cache.py`; 143 total.

### v1.2.3
- **`fetch_url` tool** — fetches any URL and returns plain text (HTML stripped). stdlib-only, no extra dependencies. Pairs with `web_search`: search first, then fetch the best result.
- **`recall_memory` tool** — selective memory search. Agents can query `memory.md` and `memory/*.md` for specific facts without loading all memory into context. Reduces dynamic suffix size for long-running sessions.
- Both tools registered as builtins and exposed via `entity/agent/tools/`.
- 10 new tests in `test_fetch_url.py`; 135 total.

### v1.2.2
- **`spawn_session` tool** — agents can now create new sub-sessions dynamically. Initialises from any entity, optionally writes an initial message, and registers on disk for `nutshell-server` to pick up automatically. Use `send_to_session` to communicate with the spawned session.
- **`nutshell.runtime.session_factory`** — extracted shared `init_session()` logic, used by both the web UI and the new tool. DRY refactor of `ui/web/sessions.py`.
- **`.gitignore`** — added `_entity_updates/`.
- 8 new tests in `test_spawn_session.py`; 125 total.

### v1.2.1
- **`propose_entity_update` tool** — agents can now submit requests to modify their own global entity files (system prompt, skills, tool schemas). Requests are queued in `_entity_updates/` and must be approved by a human before taking effect globally.
- **`nutshell-review-updates` CLI** — interactive review of pending entity update requests. Shows content preview, then `[a]pply / [r]eject / [s]kip / [q]uit` for each. Use `--list` to inspect without reviewing.
- **`nutshell.runtime.entity_updates`** — `list_pending_updates()`, `apply_update()`, `reject_update()`.
- **`entity/agent/tools/propose_entity_update.json`** — all agents get this tool by default.
- 10 new tests in `test_entity_update.py`; 117 total.

### v1.2.0
- **Anthropic prompt caching** — system prompt now split into a static prefix (system.md + session context) and a dynamic suffix (memory + skills). Anthropic provider sends the static prefix with `cache_control: {"type": "ephemeral"}`, saving ~90% of static-prefix token costs on cache hits. Kimi provider concatenates both parts as before (no cache_control).
- **`Agent._build_system_parts()`** — new internal method returning `(static_prefix, dynamic_suffix)`. `_build_system_prompt()` stays backward-compatible.
- **`Provider.complete(cache_system_prefix="")`** — new optional parameter on the Provider ABC. Concrete implementations use it to control caching behaviour.

### v1.1.9
- **`nutshell-chat` CLI** — single-shot agent interaction from terminal. `nutshell-chat "message"` creates a new session (self-contained daemon, no server needed); `--session <id>` continues an existing one. Always prints agent response; new sessions also print `Session: <id>`. Supports `--no-wait`, `--timeout`, `--entity`.
- **`send_to_session` system tool** — agents can message other sessions by ID. `mode=sync` blocks until the target replies; `mode=async` fires-and-forgets. Self-call detection, clear timeout errors.
- **`user_input_id` in turn events** — `turn` events now carry `user_input_id` linking the response to its triggering message, eliminating heartbeat-turn confusion for CLI and multi-agent polling.
- **`stop_event` in `run_daemon_loop`** — clean CLI shutdown without `CancelledError`.

### v1.1.8
- **`as_tool(clear_history=True)`** — persistent agents can now act as stateless workers in multi-agent pipelines. Pass `clear_history=True` to `as_tool()` to reset sub-agent history on each tool invocation.
- **`reload_capabilities` summary** — tool now returns a detailed summary of loaded tools and skills (e.g., `"Tools (3): bash, web_search, fetch_url. Skills (1): creator-mode."`) instead of a bare confirmation.
- **`nutshell-new-agent` parent validation** — creating an agent with `--extends <parent>` now fails immediately with a clear error if the parent entity does not exist, rather than silently creating a broken entity.
- **Memory layer cleanup on reload** — removing a file from `core/memory/` is now reflected immediately on the next `reload_capabilities` call; stale layers no longer persist.

### v1.1.7
- **Anthropic thinking block support** — `AnthropicProvider` now surfaces extended thinking: streaming path forwards `thinking_delta` events via `on_text_chunk`; non-streaming fallback extracts thinking from final message content blocks. Thinking appears in the UI thinking bubble, not in `content_text`.
- **Layered session memory** — `core/memory/*.md` files are loaded as named memory layers. Each non-empty file becomes a `## Memory: {stem}` section in the system prompt, sorted alphabetically. Coexists with primary `memory.md`. Backward-compatible: old sessions without `core/memory/` are unaffected.

### v1.1.6
- **System prompt optimization** — `session.md` reduced from 176 lines to ~20 lines (table format). Detailed bash examples (task board, memory, params) moved into `creator-mode` skill (lazy-loaded on demand). Saves ~2000 tokens per activation without losing any information.

### v1.1.5
- **`_sessions/` added to `.gitignore`** — session runtime data (context, events, status) no longer committed.

### v1.1.4
- **Fix stale imports** — `watcher.py` and `ui/web/sessions.py` were importing from `nutshell.runtime.provider_factory` (removed in v1.1.2); updated to `nutshell.llm_engine.registry`.
- **Fix web UI path** — `ui/web/app.py` was resolving sessions/`_sessions` one level above the repo root (`agent_core/`) instead of inside the repo root; removed one extra `.parent` so both server and web UI use the same directories.

### v1.1.3
- **`session.md` replaces `session_context.md`** — the session operational guide (task board, memory, skills, tools, params, playground conventions) is now a first-class prompt file with `{session_id}` substituted at load time via `str.replace()` (replaces `.format()`, safe for JSON code examples). Old sessions with `session_context.md` continue to work via automatic fallback.
- **`system.md` restructured** — agent identity and capability declaration only; all session-specific file documentation moved to `session.md`.
- **Playground conventions** — `playground/` now has documented subdirectory conventions: `tmp/` (scratch), `projects/` (multi-session), `output/` (user-facing artifacts).

### v1.1.2
- **System prompt assembly moved to `core/agent.py`** — `Agent` now owns all prompt composition: base + `session_context` + `memory` + skills. `runtime/session.py` only reads files and assigns the three fields; zero string formatting in runtime.
- **Tool taxonomy clarified** — system tools (bash, web_search) are Python-implemented, system-only, not agent-creatable. Agent-created tools are always shell-backed (`.json` + `.sh`); agents may call Python inside `.sh` scripts, but the executor is always `ShellExecutor`. Removed misleading `PythonExecutor` and `HttpExecutor` placeholders.
- **`skill_engine/renderer.py`** — skill rendering extracted from `core/agent.py` into `skill_engine/`. `build_skills_block()` handles file-backed catalog and inline injection.

### v1.1.1
- **Remove `abstract/` and `providers/`** — `Provider` ABC moved to `core/provider.py`; `BaseLoader` ABC moved to `core/loader.py`. No more shim layers.

### v1.1.0
- **`tool_engine/`** — new unified tool execution layer: `executor/` hierarchy (`BashExecutor`, `ShellExecutor`), `providers/web_search/` (Brave + Tavily), merged `registry.py`, `ToolLoader`, `reload_capabilities` tool factory.
- **`llm_engine/`** — self-contained LLM provider layer: `providers/` (Anthropic, Kimi), `registry.py`, `AgentLoader`.
- **`skill_engine/`** — `SkillLoader` extracted from `runtime/`.
- **`reload_capabilities` built-in tool** — agents can hot-reload tools and skills mid-session without restarting.
- **`creator-mode` skill** — guides agents through the tool/skill creation and iteration loop.
- **`runtime/`** slimmed to pure orchestration (session, server, watcher, IPC). All loader/provider code moved to engine packages.

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
- **Default tool provider** — `DEFAULT_PARAMS` now sets `tool_providers: {\"web_search\": \"brave\"}` explicitly; `session_context.md` documents available providers.

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
