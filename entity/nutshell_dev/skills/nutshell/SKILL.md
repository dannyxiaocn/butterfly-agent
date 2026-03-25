---
name: nutshell
description: "Full development context for the nutshell project. Use this skill for any task involving nutshell: writing code, adding features, fixing bugs, running tests, bumping versions, updating docs, simplifying the codebase, or understanding architecture. Load whenever working on this repo."
---

# Nutshell — Developer Skill

Complete workbench for developing nutshell.

Current version: **v1.2.6** | Tests: `pytest tests/ -q` (146 passing)

---

## SOPs

### 1. After Any Code Change
```bash
pytest tests/ -q          # must pass before anything else
```
Then:
1. Update `README.md` — relevant section + new Changelog entry under `## Changelog`
2. Bump version in **both** `pyproject.toml` (`version = "X.Y.Z"`) **and** `README.md` heading
3. Commit: `git commit -m "vX.Y.Z: {short summary}\n\n- detail 1\n- detail 2"`
4. Push: `git push`

**Versioning:**
- Patch (1.x.Z): bug fixes
- Minor (1.X.0): new features, backward compatible
- Major (X.0.0): breaking changes

### 2. Adding a Built-in Tool

1. Implement `nutshell/tool_engine/providers/<name>.py` — expose `async <name>(**kwargs) -> str`
2. Register in `_BUILTIN_FACTORIES` in `nutshell/tool_engine/registry.py`
3. Add `entity/agent/tools/<name>.json` (JSON schema)
4. **Add `- tools/<name>.json` to `entity/agent/agent.yaml` tools list** ← CRITICAL: omitting this means sessions never get the tool
5. Write `tests/test_<name>.py`
6. Run full SOP

**Registry pattern:**
```python
# in registry.py
_BUILTIN_FACTORIES: dict[str, Callable[[], Tool]] = {
    "my_tool": lambda: Tool(name="my_tool", description="...", fn=my_module.my_tool),
    ...
}
```

### 3. Adding a New LLM Provider

1. Implement `nutshell/llm_engine/providers/<name>.py` extending `Provider`
2. Register in `nutshell/llm_engine/registry.py` `_PROVIDERS` dict
3. `complete()` must accept `on_text_chunk=None`, `cache_system_prefix=""`, `cache_last_human_turn=False` kwargs

### 4. Adding a New Entity

```bash
nutshell-new-agent -n <name>   # interactive scaffolder, validates parent exists
```

Then edit `entity/<name>/agent.yaml`.

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
│   └── renderer.py        — build_skills_block(): catalog vs inline injection
└── runtime/
    ├── session.py         — Session: chat(), tick(), run_daemon_loop(stop_event=), _load_session_capabilities()
    ├── ipc.py             — FileIPC: context.jsonl + events.jsonl; send_message() → msg_id
    ├── status.py          — status.json r/w
    ├── params.py          — params.json: DEFAULT_PARAMS, read/write/ensure_session_params
    ├── env.py             — load_dotenv(): cwd/.env then repo-root/.env
    ├── session_factory.py — init_session(): idempotent, copies entity → core/
    ├── entity_updates.py  — list_pending_updates(), apply_update(id), reject_update(id)
    ├── watcher.py         — SessionWatcher: polls _sessions/ for new sessions
    └── server.py          — nutshell-server entry point

ui/                        (NOT inside nutshell/ package)
├── web/
│   ├── app.py             — FastAPI: SSE streaming, /api/sessions/* routes
│   ├── sessions.py        — _init_session, _read_session_info, _sort_sessions
│   └── index.html         — frontend (HTML + CSS + JS, no build step)
├── cli/
│   └── chat.py            — nutshell-chat: single-shot CLI + inline daemon
└── dui/
    └── new_agent.py       — nutshell-new-agent: interactive entity scaffolder
```

---

## Entity Inheritance

```
entity/agent/        — base: claude-sonnet-4-6, anthropic, tools: bash+web_search+built-ins
  ↑ entity/kimi_agent/   — kimi provider/model, all else inherited
    ↑ entity/nutshell_dev/ — extra skill: nutshell (this)
```

`null` fields = inherit parent. `[]` = explicitly empty. Explicit list = child-first file resolution.

**Built-in tools** (always registered regardless of entity.yaml):
`bash`, `web_search`, `send_to_session`, `spawn_session`, `propose_entity_update`, `fetch_url`, `recall_memory`, `reload_capabilities`

---

## Session Disk Layout

```
sessions/<id>/                  ← agent-visible
  core/
    system.md                   ← system prompt (copied from entity at creation)
    heartbeat.md                ← heartbeat prompt
    session.md                  ← path reference table (~20 lines)
    memory.md                   ← persistent memory (injected every activation)
    memory/                     ← layered memory: *.md → "## Memory: {stem}"
    tasks.md                    ← task board (non-empty → triggers heartbeat)
    params.json                 ← runtime config (SOURCE OF TRUTH)
    tools/                      ← .json + .sh agent-created tools
    skills/                     ← <name>/SKILL.md session skills
  docs/                         ← user uploads (read-only by convention)
  playground/                   ← free workspace (tmp/, projects/, output/)

_sessions/<id>/                 ← system-only
  manifest.json                 ← STATIC: entity, created_at
  status.json                   ← DYNAMIC: model_state, status, last_run_at, pid
  context.jsonl                 ← user_input + turn events (IPC)
  events.jsonl                  ← runtime/UI events
```

**params.json defaults:**
```json
{
  "heartbeat_interval": 600.0,
  "model": null,
  "provider": null,
  "tool_providers": {"web_search": "brave"}
}
```

---

## Key API Notes

- **`status.py` / `ipc.py`** take `system_dir` (`_sessions/<id>/`)
- **`params.py`** takes `session_dir` (`sessions/<id>/`)
- **Prompt caching**: static (system.md + session.md) cached via `cache_control`; dynamic (memory + skills) not cached
- **`session_factory.init_session()`** — shared init logic; called by `spawn_session`, `nutshell-chat`, Web UI

---

## Running Tests

```bash
pytest tests/ -q                     # all tests
pytest tests/test_<name>.py -v       # specific module
pytest tests/ -q -k "keyword"        # filter by name
```

Test files:
- `test_agent.py`, `test_agent_loader_inheritance.py`
- `test_anthropic_provider.py` (thinking block streaming)
- `test_bash_tool.py`, `test_tools.py`
- `test_cli_chat.py` (nutshell-chat new/continue/no-wait)
- `test_ipc.py`, `test_session_capabilities.py`
- `test_new_agent.py`, `test_reload_tool.py`
- `test_send_to_session.py`, `test_spawn_session.py`
- `test_fetch_url.py`, `test_entity_update.py`, `test_prompt_cache.py`

---

## Known Technical Debt

| File | Issue | Priority |
|------|-------|----------|
| `tool_engine/providers/web_search/brave.py` + `tavily.py` | `_SCHEMA` dict identical in both | LOW |
| `runtime/watcher.py` | Polls `_sessions/` every second; no inotify | LOW |
| `entity/nutshell_dev/agent.yaml` | References `session_context` prompt key (legacy) | LOW |
