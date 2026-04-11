# Session Engine — Implementation

## Files

| File | Purpose |
|------|---------|
| `entity_config.py` | `AgentConfig` dataclass — reads `agent.yaml`, parses inheritance metadata |
| `agent_loader.py` | `AgentLoader` — builds `Agent` from entity dir, resolving extends chain |
| `entity_state.py` | Meta session lifecycle, alignment, gene commands, entity↔meta sync |
| `session_init.py` | `init_session()` — creates full session directory structure from entity |
| `session_params.py` | Reads/writes `core/params.json` with defaults |
| `session_status.py` | Reads/writes `_sessions/<id>/status.json` |
| `task_cards.py` | Per-task `.md` files in `core/tasks/` with YAML frontmatter |
| `session.py` | `Session` class — wraps Agent with persistent file-backed behavior |

## Session.run_daemon_loop()

```
loop (0.5s sleep):
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
5. Write `params.json` from entity's `agent.yaml`
6. Seed memory from meta → entity fallback
7. Seed playground, task cards

## AgentLoader.load()

Recursively walks `extends` chain:
1. Read `agent.yaml` → `AgentConfig`
2. If `extends` → recursively load parent
3. Resolve prompts: child has key → use it; empty → inherit parent
4. Resolve tools/skills: `None` → inherit; explicit → resolve from ancestor dirs
5. Resolve model/provider: child → parent → hardcoded last-resort fallback `claude-sonnet-4-6/anthropic` (only reached when no entity or parent sets a model)

## Session Types

| Type | Behavior |
|------|----------|
| `ephemeral` | Auto-stops after processing inputs with no pending cards |
| `default` | Standard session, no autonomous heartbeat |
| `persistent` | Has recurring heartbeat task card at `heartbeat_interval` |

## Important Behaviors

- Every session gets its own `.venv` under `sessions/<id>/.venv`
- `reload_capabilities` tool is always injected at runtime
- Meta alignment: if entity config diverges from synced meta, child sessions blocked until resolved
- Legacy `default_task` values are migrated into `core/tasks/heartbeat.md` card
