---
name: nutshell
description: "Full development context for the nutshell project. Use this skill for any task involving nutshell: writing code, adding features, fixing bugs, running tests, bumping versions, updating docs, or understanding architecture. Load whenever working on this repo."
---

# Nutshell — Developer Skill

Complete workbench for developing nutshell.

Current version: **v1.3.4** | Tests: `pytest tests/ -q` (184 passing)

---

## Role

**You are nutshell_dev.** Claude Code dispatches tasks to you; you execute them.
- Claude Code selects tasks from `track.md`, sends you instructions, reviews your output
- You implement, test, commit — then report the commit ID back
- If you find bugs or missing features mid-task, fix them and add new `[ ]` items to `track.md`

---

## SOPs

### 1. After Any Code Change
```bash
pytest tests/ -q          # must pass before anything else
```
Then:
1. Update `README.md` — relevant section + new Changelog entry
2. Bump version in **both** `pyproject.toml` AND `README.md` heading
3. Commit: `git commit -m "vX.Y.Z: {short summary}\n\n- detail\nCo-Authored-By: ..."`
4. Report commit ID back to Claude Code

**Versioning:** Patch (Z): bug fix · Minor (Y): new feature · Major (X): breaking

### 2. track.md Workflow

`track.md` is the project task board. Keep it up to date:
- Mark completed items `[x]` with the commit ID as `<!-- COMMIT_ID vX.Y.Z -->`
- Add new `[ ]` sub-items when you discover tasks can be further broken down
- Add new `[ ]` todos when you hit missing features or related improvements

```bash
cat track.md              # read current tasks
# edit track.md to mark done or add todos
git add track.md && git commit -m "track: ..."
```

### 3. Adding a Built-in Tool

1. `nutshell/tool_engine/providers/<name>.py` — expose `async <name>(**kwargs) -> str`
2. Register in `_BUILTIN_FACTORIES` in `nutshell/tool_engine/registry.py`
3. Add `entity/agent/tools/<name>.json` (JSON schema)
4. **Add to `entity/agent/agent.yaml` tools list** ← CRITICAL: omitting = sessions never get the tool
5. Write `tests/test_<name>.py`
6. Run full SOP

### 4. Adding a New LLM Provider

1. `nutshell/llm_engine/providers/<name>.py` extending `Provider`
2. Register in `nutshell/llm_engine/registry.py`
3. `complete()` returns `(str, list[ToolCall], TokenUsage)` — 3-tuple
4. `complete()` accepts `on_text_chunk=None`, `cache_system_prefix=""`, `cache_last_human_turn=False`

---

## Package Layout

```
nutshell/
├── core/                  — ABCs + Agent, Tool, Skill, Provider, types
│   ├── agent.py           — Agent: run(), _history, _build_system_parts(), memory + memory_layers
│   ├── tool.py, skill.py, provider.py, types.py
│   └── loader.py          — AgentLoader (inheritance chain resolution)
├── llm_engine/
│   ├── providers/
│   │   ├── anthropic.py   — AnthropicProvider: streaming, thinking, cache_control
│   │   └── kimi.py        — KimiProvider: Anthropic-compatible, no cache_control
│   ├── registry.py        — resolve_provider(name), provider_name(provider)
│   └── loader.py
├── tool_engine/
│   ├── executor/          — base.py, bash.py (subprocess/PTY), shell.py
│   ├── providers/
│   │   ├── web_search/    — brave.py, tavily.py
│   │   ├── session_msg.py — send_to_session: sync/async cross-session messaging
│   │   ├── spawn_session.py — spawn_session: creates session from entity
│   │   ├── entity_update.py — propose_entity_update: entity change requests
│   │   ├── fetch_url.py   — fetch_url: stdlib URL fetcher, HTML stripping
│   │   └── recall_memory.py — recall_memory: keyword search in memory files
│   ├── registry.py        — _BUILTIN_FACTORIES + get_builtin(name)
│   ├── loader.py          — ToolLoader: .json + built-in registry + .sh shell tools
│   ├── reload.py          — create_reload_tool(): hot-reload core/ capabilities
│   └── sandbox.py
├── skill_engine/
│   ├── loader.py          — SkillLoader: SKILL.md + flat .md
│   └── renderer.py        — build_skills_block()
└── runtime/
    ├── session.py         — Session: chat(), tick(), run_daemon_loop(stop_event=)
    ├── ipc.py             — FileIPC: context.jsonl + events.jsonl; send_message() → msg_id
    ├── status.py          — status.json r/w
    ├── params.py          — params.json: DEFAULT_PARAMS, read/write/ensure_session_params
    ├── session_factory.py — init_session(): copies entity → core/ (skills, tools, memory)
    ├── entity_updates.py  — list_pending_updates(), apply_update(id), reject_update(id)
    └── server.py          — nutshell-server entry point

ui/                        (NOT inside nutshell/ package)
├── cli/
│   ├── main.py            — nutshell: unified CLI entry point
│   └── chat.py            — nutshell-chat legacy entry point
└── web/                   — FastAPI + SSE monitoring UI
```

---

## CLI (v1.3.4)

```bash
# Session management (no server required)
nutshell sessions                     # list all sessions
nutshell sessions --json              # JSON output
nutshell new [ID] [--entity NAME]     # create session
nutshell stop SESSION_ID              # pause heartbeat
nutshell start SESSION_ID             # resume heartbeat
nutshell tasks [SESSION_ID]           # show session's tasks.md
nutshell log [SESSION_ID] [-n N]      # show last N conversation turns

# Messaging
nutshell chat "message"               # new session + send
nutshell chat --session ID "message"  # continue session
nutshell chat --session ID --no-wait "message"  # fire-and-forget

# Entity management
nutshell entity new -n NAME           # scaffold new entity
nutshell review                       # review agent entity-update requests
```

---

## Entity Inheritance

```
entity/agent/        — base: claude-sonnet-4-6, anthropic
  ↑ entity/kimi_agent/   — kimi provider/model
    ↑ entity/nutshell_dev/ — adds: nutshell skill, memory.md pre-seeded
```

**Built-in tools** (always available):
`bash`, `web_search`, `send_to_session`, `spawn_session`, `propose_entity_update`,
`fetch_url`, `recall_memory`, `reload_capabilities`

---

## Session Disk Layout

```
sessions/<id>/core/
  system.md        ← system prompt
  memory.md        ← persistent memory (seeded from entity on first creation)
  memory/          ← layered memory: *.md → "## Memory: {stem}" blocks
  tasks.md         ← task board (non-empty triggers heartbeat)
  params.json      ← runtime config (SOURCE OF TRUTH)
  tools/           ← .json + .sh agent-created tools
  skills/          ← <name>/SKILL.md session skills

_sessions/<id>/
  context.jsonl    ← user_input + turn events
  status.json      ← DYNAMIC: model_state, status, pid
```

---

## Key API Notes

- **`status.py` / `ipc.py`** take `system_dir` (`_sessions/<id>/`)
- **`params.py`** takes `session_dir` (`sessions/<id>/`)
- **Prompt caching**: static (system.md + session.md) cached; dynamic (memory + skills) not cached
- **`session_factory.init_session()`** — copies entity memory.md → core/memory.md on first creation

---

## Running Tests

```bash
pytest tests/ -q                     # all (184 tests)
pytest tests/test_<name>.py -v       # specific module
pytest tests/ -q -k "keyword"        # filter
```

---

## Known Technical Debt

| File | Issue | Priority |
|------|-------|----------|
| `tool_engine/providers/web_search/brave.py` + `tavily.py` | `_SCHEMA` dict identical | LOW |
| `runtime/watcher.py` | Polls every second; no inotify | LOW |
