# `butterfly.runtime.io` — the only read/write surface

One module. Every read and every write for a session flows through it. The web backend and the CLI both import `from butterfly.runtime import io` and call nothing else. No file IO lives anywhere else. Tests exercise the module directly.

## Session resolution

Callers pass `session_id` (a string). The module resolves paths internally:

- `_resolve_session_dir(session_id)` — returns the *system* directory, preferring live `_sessions/<id>/` then falling back to `_archived/<id>/`. Raises `FileNotFoundError` if neither exists. The system dir holds `manifest.json`, `status.json`, `events_v1.jsonl`.
- `_resolve_user_dir(session_id)` — returns `sessions/<id>/`, the user-content dir (task cards, `core/config.yaml`, prompts, panel entries). May not exist for archived sessions.

`session_id` is validated against `^[\w\-]+$`; anything else raises `ValueError`.

## Readers

```python
list_sessions(include_archived: bool = False) -> list[SessionInfo]
get_session(session_id) -> SessionInfo
get_status(session_id) -> SessionStatus

read_events(session_id, *, since_id=None, until_id=None, types=None) -> Iterator[Event]
latest_event_id(session_id) -> int
tail_events(session_id, *, cursor=None, timeout=30.0, poll_interval=0.1) -> AsyncIterator[Event]

read_llm_context(session_id) -> list[Message]
read_display_history(session_id, *, since_id=0) -> list[Event]
read_hud(session_id) -> HudSnapshot
read_task_cards(session_id) -> list[dict]
read_todo_list(session_id) -> TodoList | None
read_config(session_id) -> dict
read_prompt(session_id, name) -> str
read_asset(session_id, name) -> str
read_panel(session_id) -> list[PanelEntry]
read_panel_entry(session_id, tid) -> PanelEntry
read_terminal_state(session_id) -> TerminalState
read_terminal_log(session_id, ...) -> list[TerminalLogEntry]
list_models() -> list[dict]
list_agents() -> list[str]
```

`read_events` / `latest_event_id` / `tail_events` are thin session_id wrappers over the primitives in `butterfly.runtime.events` — documented in [events.md](events.md).

`read_llm_context(session_id)` is exactly `build_llm_context(read_events(session_id))`. That is the ONLY path that produces the message list handed to `provider.complete()`. See `butterfly/runtime/llm_context.py` for the builder contract.

`read_display_history` returns the event list the web transcript replays — every `user_*` and `agent_*` event plus the UI-visible system subset (`model_status`, `llm_call_usage`, `task_*`, `todo_list_changed`, `tool_progress`, `terminal_*`, `panel_entry_changed`, `sub_agent_count`, `config_changed`, `prompt_changed`, `asset_changed`, `system_notice`, `error`). Plumbing events like `control_*` and `session_started` are filtered out.

## Writers

Every writer appends one event (§events.md "Write semantics"), then materialises any derived file.

```python
create_session(session_id, *, agent="default", display_name=None, init_from=None) -> SessionInfo
delete_session(session_id) -> Event
start_session(session_id) -> Event            # emits control_start
stop_session(session_id, *, reason="user") -> Event

send_message(session_id, text, *, source="cli", caller=None, display_name=None) -> Event
interrupt_session(session_id, *, text=None) -> Event     # emits control_interrupt

upsert_task(session_id, name, *, description=None, script=None, check_interval=None, notes=None, progress=None) -> Event
delete_task(session_id, name) -> Event
upsert_todo_list(session_id, todo_list) -> Event
kill_panel_entry(session_id, tid) -> Event

terminal_input(session_id, text, *, source="web") -> Event
terminal_interrupt(session_id) -> Event

update_config(session_id, key, value) -> Event
update_prompt(session_id, name, content) -> Event
update_asset(session_id, name, content) -> Event
```

Each writer returns the persisted `Event` so callers (tests, CLI) can cursor-advance.

## Error model

| Raise | Meaning |
|---|---|
| `FileNotFoundError` | `session_id` not found in live or archived tree; or named prompt/asset absent |
| `ValueError` | Malformed `session_id`, invalid arg shape, unknown key, schema violation |
| `IOError` | Disk write failed (flock contention, EIO). Rare; surfaced to the caller |
| `NotImplementedError` | Reader/writer stub that hasn't been implemented yet |

The web layer maps `FileNotFoundError` → HTTP 404, `ValueError` → 400, everything else → 500. The CLI prints `Error: {exc}` to stderr and exits 2.

## Invariants

- I3: `read_llm_context(session_id)` is the only code path that computes LLM messages.
- I5: The web UI never writes to a session file; every write goes through a function in this module.
- I6: Every writer here is reachable from the CLI (`butterfly io <fn>` reflection, plus friendly aliases). Adding a writer without a CLI surface is a bug.
