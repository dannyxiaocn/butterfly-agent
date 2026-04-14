# Session Engine — Implementation

## Files

| File | Purpose |
|------|---------|
| `entity_config.py` | `AgentConfig` dataclass — reads `config.yaml`, provides typed view of manifest |
| `agent_loader.py` | `AgentLoader` — builds `Agent` from a fully self-contained entity dir |
| `entity_state.py` | Meta session lifecycle, version management, gene commands, entity→meta bootstrap |
| `session_init.py` | `init_session()` — creates full session directory structure from meta session |
| `session_config.py` | Reads/writes `core/config.yaml` with defaults |
| `session_status.py` | Reads/writes `_sessions/<id>/status.json` |
| `task_cards.py` | Per-task `.json` files in `core/tasks/` with scheduling and status management |
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
  1. Build prompt from card + task.md
  2. agent.run(...)
  3. Mark card done (recurring → pending with updated last_finished_at)
  4. On error: mark_pending() (not paused) so task retries next cycle
```

## init_session() Flow

1. Create `sessions/<id>/core/` + `_sessions/<id>/`
2. Write `manifest.json`, create `.venv`
3. Ensure meta session → `populate_meta_from_entity()` if first time
4. Copy prompts/tools/skills **from meta** (not directly from entity)
5. Write `config.yaml` from entity's `config.yaml`; record meta version as `agent_version`
6. Seed memory from meta → entity fallback
7. Seed playground, task cards

## AgentLoader.load()

Each entity is fully self-contained — no inheritance chain:
1. Read `config.yaml` → `AgentConfig`
2. Load prompts from paths listed under `prompts:` key
3. Load tools from paths listed under `tools:` key
4. Load skills from paths listed under `skills:` key
5. Resolve model/provider from manifest; fall back to `claude-sonnet-4-6/anthropic` if absent

## Version Management

- Meta session version: `agent_version` in `sessions/<entity>_meta/core/config.yaml`
- Child session records meta version at creation time in its own `core/config.yaml`
- `Session._emit_version_notice_if_stale()` emits a `system_notice` event if meta has advanced
- `bump_meta_version()` increments patch version and appends to history

## Session Types

| Type | Behavior |
|------|----------|
| `ephemeral` | Auto-stops after processing inputs with no pending cards |
| `default` | Standard session, no autonomous tasks |
| `persistent` | Has recurring task card (e.g. duty) with configured interval |

## Task Card System

Each task card is a `.json` file in `core/tasks/`:

```json
{
  "name": "duty",
  "description": "Review and process child sessions",
  "status": "pending",
  "interval": 3600,
  "start_at": "2026-04-12T11:00:00",
  "end_at": "2026-04-19T10:00:00",
  "created_at": "2026-04-12T10:00:00",
  "last_started_at": null,
  "last_finished_at": null,
  "comments": "",
  "progress": ""
}
```

### Status values

| Status | Meaning |
|--------|---------|
| `pending` | Waiting for next trigger (default state for new and recurring tasks) |
| `working` | Currently being executed |
| `finished` | Completed (one-shot) or manually finished |
| `paused` | User-initiated pause; won't fire until explicitly resumed |

### Scheduling (`start_at` / `end_at`)

- `start_at`: earliest time a task can fire. Default for recurring = `ceil(created_at + interval)`; for one-shot = `floor(created_at)`.
- `end_at`: auto-expire time. Default = `ceil(created_at + 7 days)`; if interval > 7 days then `ceil(created_at + 10 * interval)`.
- Hour-level granularity: `_ceil_to_hour()` rounds up, `_floor_to_hour()` truncates down.
- A task with `status=pending` fires when: `now >= start_at AND now < end_at AND (never finished OR interval elapsed)`.
- Past `end_at` → auto-marked `finished` and persisted to disk by `load_due_cards()`.

## Important Behaviors

- Every session gets its own `.venv` under `sessions/<id>/.venv`
- `reload_capabilities` tool is always injected at runtime
- `system_notice` events are passed through IPC and rendered in both web UI and SSE stream
