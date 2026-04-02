# Nutshell — Claude Code Context

> Current version: **v1.3.46** · Python package · `pip install -e .`

Minimal Python agent runtime. Core value: **simplicity + persistence**.  
Agents run as long-lived server-managed sessions with file-based IPC.

---

## Architecture overview

```
nutshell/
├── core/           ABCs + Agent, Tool, Skill, Provider, types, loader
├── llm_engine/     LLM providers: Anthropic (+ thinking), Kimi, OpenAI, Codex
├── tool_engine/    Bash/shell executors, web_search, built-in tools, sandbox
├── skill_engine/   SKILL.md loader + system-prompt renderer
└── runtime/        session.py, session_factory.py, ipc.py, watcher.py,
                    meta_session.py, status.py, params.py, server.py

ui/                 (repo root — NOT inside nutshell/)
├── web/            FastAPI + SSE web UI at http://localhost:8080
│   ├── app.py
│   ├── index.html
│   ├── sessions.py
│   └── weixin.py   WeChat ↔ Nutshell bridge
└── cli/
    ├── main.py     unified `nutshell` CLI (~1400 lines)
    └── chat.py

entity/             entity definitions (agent.yaml + prompts/ + tools/ + skills/)
sessions/           agent-visible session dirs (writable by agents)
_sessions/          system-only session dirs (manifest, status, context, events)
```

## Key design decisions

- **Filesystem-As-Everything**: agents read/write their own session dir; UI and server communicate via `context.jsonl` + `events.jsonl` — no sockets.
- **Meta-session layer** (`sessions/<entity>_meta/`): entity-level mutable state. Configs always come from meta; entity/ is read-only config source.
- **Meta-session as real agent**: `_sessions/<entity>_meta/` system dir = watcher picks it up as a running agent (dream cycle = 6h heartbeat).
- **Session venv**: each session (and meta session) gets `--system-site-packages` venv at `<session>/.venv`; agents can `pip install` freely.
- **Gene feature**: `gene:` field in `agent.yaml` = shell commands run once in meta playground on first entity init.

## Disk layout per session

```
sessions/<id>/          ← agent-visible
  core/
    system.md           system prompt
    heartbeat.md        heartbeat prompt
    session.md          session paths guide (template)
    memory.md           persistent memory (auto-injected)
    memory/             named memory layers (*.md)
    tasks.md            task board
    params.json         runtime config (model, provider, heartbeat_interval, ...)
    tools/              tool definitions (.json + .sh)
    skills/             skill dirs
  docs/                 user-uploaded files
  playground/           agent's free workspace
  .venv/                session-isolated Python venv

_sessions/<id>/         ← system-only
  manifest.json         entity, created_at
  status.json           dynamic runtime state (status, pid, heartbeat_interval)
  context.jsonl         conversation history ("turn" + "user_input" events)
  events.jsonl          runtime/UI events (streaming text, tool calls)
```

## Running & testing

```bash
# Start server + web UI
nutshell server          # starts watcher + web at :8080
nutshell web             # web UI only

# Chat
nutshell chat "message" [--entity NAME] [--session ID] [--timeout N]

# Manage sessions
nutshell sessions        # list
nutshell new [ID] [--entity NAME]
nutshell stop/start SESSION_ID
nutshell log SESSION_ID [-n 10] [--watch]
nutshell tasks SESSION_ID

# Meta / dream
nutshell meta [ENTITY]          # show meta session
nutshell meta ENTITY --init     # force re-run gene commands
nutshell dream ENTITY           # send "看任务来执行" to meta session

# Tests
pytest tests/ -q                # 762 tests, ~60s
pytest tests/runtime/ -q        # runtime only
```

## Entity definition (agent.yaml)

```yaml
name: my_agent
extends: agent           # inherit tools/skills from parent
model: claude-sonnet-4-6
provider: anthropic      # anthropic | openai | kimi-coding-plan
gene:
  - "pip install requests"    # run once in meta playground on init
params:
  persistent: true            # stay alive between heartbeats
  heartbeat_interval: 3600    # seconds
  default_task: "Check inbox" # task to run when tasks.md is empty
  blocked_patterns:           # sandbox: reject bash commands matching these
    - "rm -rf /"
prompts:
  - system.md
  - heartbeat.md
  - session.md
```

## LLM providers

| Key | Class | Env var |
|-----|-------|---------|
| `anthropic` | `AnthropicProvider` | `ANTHROPIC_API_KEY` |
| `openai` | `OpenAIProvider` | `OPENAI_API_KEY`, `OPENAI_BASE_URL` (opt) |
| `kimi-coding-plan` | `KimiForCodingProvider` | Kimi API key |

Switch in session: edit `sessions/<id>/core/params.json` → `{"provider": "openai", "model": "gpt-4o"}`.

## Built-in tools (agent entity, 11 tools)

`bash`, `web_search`, `send_to_session`, `spawn_session`, `propose_entity_update`,  
`fetch_url`, `recall_memory`, `state_diff`, `git_checkpoint`, `app_notify`, `update_meta_memory`

`reload_capabilities` is always injected by `_load_session_capabilities()` (not from YAML).

## System prompt structure + caching

Order: `system.md` → `session.md` → `memory.md` → `memory_layers` → `app_notifications` → skills catalog

Static prefix (`system.md` + `session.md`) cached with `cache_control: {"type": "ephemeral"}` for Anthropic.  
Memory layers >60 lines are truncated in prompt (bash hint for remainder).

## context.jsonl event schema

```jsonc
// User message
{"type": "user_input", "content": "...", "id": "<uuid>", "caller": "human|agent", "ts": "..."}

// Agent turn
{"type": "turn", "triggered_by": "user|heartbeat", "messages": [...], "ts": "...",
 "user_input_id": "<uuid>",   // links to triggering user_input
 "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}}
```

## Meta-alignment system

When a session starts, `check_meta_alignment()` compares entity config vs meta config.  
If entity has content that differs from meta → `MetaAlignmentError` → session blocked (`alignment_blocked` status).

Fix: `nutshell meta ENTITY --sync entity-wins` (or `meta-wins`).

Only flags diffs when **entity has non-empty content** — meta's built-in prompts don't trigger false positives.

## Common development patterns

### Add a built-in tool
1. Create `nutshell/tool_engine/providers/<tool_name>.py` with `async def <tool_name>(...)` function
2. Register in `nutshell/tool_engine/registry.py`
3. Add `.json` definition to `entity/agent/tools/`
4. Run tests

### Add an LLM provider
1. Create `nutshell/llm_engine/providers/<name>_provider.py` implementing `Provider` ABC
2. Register in `nutshell/llm_engine/registry.py`

### Add a new entity
```bash
nutshell entity new my_entity
# edit entity/my_entity/agent.yaml + prompts/
```

## Gotchas

- `entity/` is **read-only config** — agents should never write there. All mutable state lives in `sessions/`.
- Meta session system dir (`_sessions/<entity>_meta/`) is created by `start_meta_agent()` on first `init_session()`. The watcher skips alignment check for meta sessions themselves.
- `session_factory.init_session()` is idempotent — safe to call multiple times for the same `session_id`.
- `Agent._history` is in-memory only; persistence comes from `context.jsonl` via `session.load_history()`.
- The `_agent_lock` in `Session` ensures heartbeat and user messages don't race — one at a time.
- WeChat bridge (`ui/web/weixin.py`) reads token from `~/.openclaw/state/openclaw-weixin/accounts/`.
- CCR scheduled trigger `trig_01FWRCYSMR6SyQ4UyKVXgYZU` runs daily at UTC 19:00 (3AM CST) for code review + CLAUDE.md update.
