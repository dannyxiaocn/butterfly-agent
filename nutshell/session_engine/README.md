# `nutshell/session_engine`

This subsystem materializes entities into runnable sessions and wraps `Agent` with persistent, file-backed session behavior.

## What This Part Is

- `entity_config.py`: reads `agent.yaml`.
- `agent_loader.py`: builds an `Agent` from an entity directory.
- `entity_state.py`: manages meta sessions, entity/meta alignment, and optional `gene` initialization.
- `session_init.py`: creates `sessions/<id>/` and `_sessions/<id>/`, seeds memory, tools, skills, playground files, and `.venv`.
- `session_params.py`: reads and writes `core/params.json`.
- `session_status.py`: reads and writes `_sessions/<id>/status.json`.
- `task_cards.py`: task card system — per-task `.md` files in `core/tasks/` with YAML frontmatter (interval, status, last_run_at).
- `session.py`: the persistent session wrapper that reloads capabilities, handles chat and task card ticks, and appends JSONL events.

## How To Use It

Programmatically:

```python
from nutshell.session_engine.session_init import init_session

init_session(session_id="demo", entity_name="agent")
```

Most users enter through the CLI:

```bash
nutshell new --entity agent
nutshell chat --entity nutshell_dev "review this repo"
```

## How It Contributes To The Whole System

- It is the bridge between static entity definitions and live runtime sessions.
- It owns the entity -> meta session -> child session lifecycle.
- It keeps session behavior editable from disk: prompts, tools, skills, memory, and params are all re-read before each activation.

## Important Behavior

- Every new session gets its own `.venv` under `sessions/<id>/.venv`.
- Meta sessions are real persistent sessions stored as `sessions/<entity>_meta/` and `_sessions/<entity>_meta/`.
- If entity config diverges from a synced meta session, child sessions can be blocked with `alignment_blocked` until resolved.
- `reload_capabilities` is injected at runtime and cannot be overridden from disk.

