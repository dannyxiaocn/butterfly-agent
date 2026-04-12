# Session Engine — Implementation

## Files

| File | Purpose |
|------|---------|
| `entity_config.py` | `AgentConfig` dataclass — reads `agent.yaml`, provides typed view of manifest |
| `agent_loader.py` | `AgentLoader` — builds `Agent` from a fully self-contained entity dir |
| `entity_state.py` | Meta session lifecycle, version management, gene commands, entity→meta bootstrap |
| `session_init.py` | `init_session()` — creates full session directory structure from meta session |
| `session_params.py` | Reads/writes `core/params.json` with defaults |
| `session_status.py` | Reads/writes `_sessions/<id>/status.json` |
| `task_cards.py` | Per-task `.md` files in `core/tasks/` with YAML frontmatter |
| `session.py` | `Session` class — wraps Agent with persistent file-backed behavior |

## Session.run_daemon_loop()

```
loop (0.5s sleep):
  ├─ _emit_version_notice_if_stale()  ← once on startup; emits system_notice if meta is newer
  ├─ poll_inputs() → new user_input → chat(message)
  ├─ poll_interrupt() → interrupt → stop current run
  └─ check due task cards → tick(card)

chat(message):
  1. _load_session_capabilities()  ← re-read core/ from disk
  2. agent.run(messages, tools)    ← LLM loop
  3. append turn to context.jsonl

tick(card):
  1. Build prompt from card + heartbeat.md
  2. agent.run(...)
  3. Mark card done (recurring → pending with updated last_run_at)
```

## init_session() Flow

1. Create `sessions/<id>/core/` + `_sessions/<id>/`
2. Write `manifest.json`, create `.venv`
3. Ensure meta session → `populate_meta_from_entity()` if first time
4. Copy prompts/tools/skills **from meta** (not directly from entity)
5. Write `params.json` from entity's `agent.yaml`; record meta version as `agent_version`
6. Seed memory from meta → entity fallback
7. Seed playground, task cards

## AgentLoader.load()

Each entity is fully self-contained — no inheritance chain:
1. Read `agent.yaml` → `AgentConfig`
2. Load prompts from paths listed under `prompts:` key
3. Load tools from paths listed under `tools:` key
4. Load skills from paths listed under `skills:` key
5. Resolve model/provider from manifest; fall back to `claude-sonnet-4-6/anthropic` if absent

## Version Management

- Meta session version: `agent_version` in `sessions/<entity>_meta/core/params.json`
- Version history: `_sessions/<entity>_meta/version_history.json`
- Child session records meta version at creation time in its own `core/params.json`
- `Session._emit_version_notice_if_stale()` emits a `system_notice` event if meta has advanced
- `bump_meta_version()` increments patch version and appends to history

## Session Types

| Type | Behavior |
|------|----------|
| `ephemeral` | Auto-stops after processing inputs with no pending cards |
| `default` | Standard session, no autonomous heartbeat |
| `persistent` | Has recurring heartbeat task card at `heartbeat_interval` |

## Important Behaviors

- Every session gets its own `.venv` under `sessions/<id>/.venv`
- `reload_capabilities` tool is always injected at runtime
- Legacy `default_task` values are migrated into `core/tasks/heartbeat.md` card
- `system_notice` events are passed through IPC and rendered in both web UI and SSE stream
