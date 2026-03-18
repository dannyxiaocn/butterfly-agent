---
name: nutshell
description: "Full development context for the nutshell project. Use this skill for any task involving nutshell: writing code, adding features, fixing bugs, running tests, bumping versions, updating docs, simplifying the codebase, or understanding architecture. Load whenever working on this repo."
---

# Nutshell — Developer Skill

This skill is your complete workbench for developing nutshell. It covers project architecture, development SOPs, and when to invoke specialist agents.

Current version: **v1.0.4** | Tests: `pytest tests/ -q` (62 passing)

---

## SOPs

### 1. After Any Code Change
```bash
pytest tests/ -q          # must pass before anything else
```
Then:
1. Update `README.md` — relevant section + new Changelog entry under `## Changelog`
2. Bump version in **both** `pyproject.toml` (`version = "X.Y.Z"`) **and** `README.md` heading (`# Nutshell \`vX.Y.Z\``)
3. Check `.gitignore` — if new dirs/files were introduced (session dirs, cache dirs, build artifacts), add them
4. Commit: `git commit -m "vX.Y.Z: {short summary}"`
5. Push: `git push`

**Versioning rules:**
- Patch (1.0.X): bug fixes, no API change
- Minor (1.X.0): new features, backward compatible
- Major (X.0.0): breaking API changes

### 2. Adding a Built-in Tool
1. Implement in `nutshell/providers/tool/<name>.py` — expose `create_<name>_tool() -> Tool`
2. Register in `nutshell/runtime/tools/_registry.py`
3. Add `entity/agent/tools/<name>.json` (JSON Schema)
4. Add to `entity/agent/agent.yaml` tools list
5. Run full SOP (tests → docs → version → commit → push)

### 3. Adding a New LLM Provider
1. Implement in `nutshell/providers/llm/<name>.py` extending `AnthropicProvider` or `Provider`
2. Register in `nutshell/runtime/provider_factory.py` `_REGISTRY`
3. Export from `nutshell/providers/llm/__init__.py`

### 4. Adding a New Tool Provider (e.g. new web_search backend)
1. Implement in `nutshell/providers/tool/<name>.py` — expose `async _<name>_search()`
2. Register in `nutshell/runtime/tool_provider_factory.py` `_REGISTRY`
3. Agent switches via `params.json`: `{"tool_providers": {"web_search": "<name>"}}`

### 5. Adding a New Entity
```bash
nutshell-new-agent -n <name>   # interactive — picks parent, scaffolds files
```
Then edit `entity/<name>/agent.yaml` to set model, provider, description.

---

## When to Use the Simplify Agent

Run the simplify agent when:
- Codebase has grown significantly (multiple new features merged)
- You notice dead code, unused imports, or duplicated logic
- A refactor left behind stale scaffolding
- The user asks to "clean up", "simplify", or "reduce code"
- After a major feature is complete and the implementation can be tightened

To invoke: spawn a subagent with instructions from `agents/simplify.md` in this skill directory.

The simplify agent will: audit all modules, remove dead code, eliminate duplication, fix obvious bugs, and verify tests still pass — without changing behaviour.

---

## Known Technical Debt

| File | Issue | Priority |
|------|-------|----------|
| ~~`ui/web.py`~~ | Refactored into `ui/web/` package in v1.0.3 | ✅ Done |
| `providers/tool/web_search.py` + `tavily.py` | `_SCHEMA` dict is identical in both files | LOW |
| `runtime/tools/_registry.py` | `get_builtin()` creates a new Tool instance on every call (no caching) | LOW |
| `runtime/watcher.py` | Polls `_sessions/` every second (O(n) scan); no file-system watch | LOW |
| `session.py:_reshape_history()` | Detects heartbeat prompts via hardcoded string "Heartbeat activation" | LOW |

---

## Project Architecture

### Core design principles
- **File-based IPC only** — no sockets; server ↔ UI communicate via `context.jsonl` + `events.jsonl`
- **Capability reload on every activation** — agent reads `core/` fresh before each run; no restart needed
- **Dual-directory sessions** — `sessions/<id>/` (agent-visible) + `_sessions/<id>/` (system-only)
- **Entity copy-on-create** — full inheritance chain resolved and copied into `core/` at session creation; entity dir never accessed at runtime

### Package layout
```
nutshell/
├── abstract/         — ABCs: BaseAgent, BaseTool, Provider, BaseLoader
├── core/
│   ├── agent.py      — Agent: LLM loop, tool dispatch, history, on_text_chunk/on_tool_call
│   ├── tool.py       — Tool + @tool decorator (auto-schema from type hints)
│   ├── skill.py      — Skill dataclass
│   └── types.py      — Message, ToolCall, AgentResult
├── providers/
│   ├── llm/
│   │   ├── anthropic.py  — AnthropicProvider (streaming, custom base_url)
│   │   └── kimi.py       — KimiForCodingProvider (extends Anthropic, KIMI_FOR_CODING_API_KEY)
│   └── tool/
│       ├── web_search.py — Brave Search (_web_search, BRAVE_API_KEY)
│       └── tavily.py     — Tavily Search (_tavily_search, TAVILY_API_KEY)
├── runtime/
│   ├── session.py         — Session: chat(), tick(), run_daemon_loop(), _load_session_capabilities()
│   ├── ipc.py             — FileIPC(system_dir): context.jsonl + events.jsonl
│   ├── status.py          — status.json: read/write_session_status(system_dir, ...)
│   ├── params.py          — params.json: DEFAULT_PARAMS, read/write/ensure_session_params(session_dir)
│   ├── provider_factory.py      — resolve_provider(name), provider_name(provider)
│   ├── tool_provider_factory.py — resolve_tool_impl(tool_name, provider_name), list_providers()
│   ├── watcher.py         — SessionWatcher: polls _sessions/
│   ├── server.py          — nutshell-server entry point
│   ├── loaders/
│   │   ├── agent.py   — AgentLoader: deep extends chain, child-first file resolution
│   │   ├── tool.py    — ToolLoader: .json + built-in registry + .sh shell tools
│   │   └── skill.py   — SkillLoader: YAML frontmatter + body
│   └── tools/
│       ├── bash.py        — create_bash_tool() subprocess + PTY modes
│       └── _registry.py   — built-in registry: {bash, web_search}
├── cli/
│   └── new_agent.py   — nutshell-new-agent: interactive entity scaffolder
└── ui/
    ├── web/           — FastAPI + SSE, http://localhost:8080
    │   ├── __init__.py    — re-exports create_app, main
    │   ├── app.py         — FastAPI routes + _sse_format() + main()
    │   ├── sessions.py    — _read_session_info, _sort_sessions, _init_session
    │   └── index.html     — frontend (HTML + CSS + JS)
    └── tui.py         — Textual TUI, nutshell-tui entry point
```

### Entities (inheritance chain)
```
agent  ←  kimi_agent  ←  nutshell_dev
```
- `entity/agent/` — base: claude-sonnet-4-6, anthropic, tools: bash+web_search, skills: skill-creator
- `entity/kimi_agent/` — kimi-for-coding, kimi-coding-plan, all else inherited
- `entity/nutshell_dev/` — extra skill: nutshell (this), all else inherited

**Inheritance rules:** `null` = inherit parent · `[]` = explicitly empty · explicit list = child-first file resolution

---

## Session Disk Layout

```
sessions/<id>/core/          ← agent reads/writes
  system.md heartbeat.md session_context.md memory.md tasks.md
  params.json                ← SOURCE OF TRUTH for runtime config
  tools/  <name>.json + <name>.sh   ← agent-created tools
  skills/ <name>/SKILL.md           ← session-level skills
sessions/<id>/docs/          ← user uploads (read-only)
sessions/<id>/playground/    ← free workspace

_sessions/<id>/              ← system-only, never touch
  manifest.json              ← STATIC: entity, created_at
  status.json                ← DYNAMIC: model_state, pid, status, last_run_at...
  context.jsonl              ← user_input + turn events
  events.jsonl               ← model_status, partial_text, tool_call...
```

### params.json defaults
```json
{
  "heartbeat_interval": 600.0,
  "model": null,
  "provider": null,
  "tool_providers": {"web_search": "brave"}
}
```
`tool_providers.web_search`: `"brave"` (default, needs `BRAVE_API_KEY`) or `"tavily"` (needs `TAVILY_API_KEY`)

### status.json fields
`model_state`: running|idle · `model_source`: user|heartbeat|system · `status`: active|stopped · `pid` · `last_run_at` · `heartbeat_interval`

---

## Key API Notes

**`status.py` / `ipc.py`** — take `system_dir` (`_sessions/<id>/`), NOT `session_dir`
**`params.py`** — takes `session_dir` (`sessions/<id>/`)
**`Session(agent, base_dir, system_base, heartbeat)`** — `session.core_dir` → `sessions/<id>/core/`, `session.system_dir` → `_sessions/<id>/`

### Built-in tools
**bash**: `command` (required), `timeout`, `workdir`, `pty` (PTY mode for interactive programs, Unix only)
**web_search**: `query` (required), `count=5`, `country`, `language`, `freshness` (day|week|month|year), `date_after`, `date_before` (YYYY-MM-DD)

### LLM providers
| Name | Class | Env Var |
|---|---|---|
| `anthropic` | `AnthropicProvider` | `ANTHROPIC_API_KEY` |
| `kimi-coding-plan` | `KimiForCodingProvider` | `KIMI_FOR_CODING_API_KEY` |

---

## Heartbeat Mechanics
- Fires every `heartbeat_interval` seconds (default 600s, read fresh from params.json each cycle)
- Skipped when: tasks.md empty · agent lock held · session stopped
- `last_tick_time` resets **after** every agent run (user or heartbeat)
- `SESSION_FINISHED` in response → clears tasks, rolls back heartbeat history
- On server restart: timer initialised from `last_run_at` in status.json
