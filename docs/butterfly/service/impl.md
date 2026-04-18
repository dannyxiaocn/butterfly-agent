# Service — Implementation

## Modules

| Module | Purpose |
|--------|---------|
| `sessions_service.py` | Session discovery, create, stop, start, delete |
| `messages_service.py` | Enqueue user messages, wait for reply, iterate events, interrupt |
| `history_service.py` | Log turns, pending inputs, prompt stats, token reports |
| `tasks_service.py` | Task card CRUD |
| `config_service.py` | Session params/config get and update — JSON and raw-YAML paths |
| `hud_service.py` | HUD summary data for web UI |
| `models_service.py` | Provider → model catalog used by the web config editor |

## Usage

```python
from butterfly.service.sessions_service import create_session, list_sessions
from butterfly.service.messages_service import send_message
```

CLI and Web should only call these functions, never access IPC/status files directly.

## `config_service` — paths

- `get_config(session_id, …)` — returns the merged dict (includes `is_meta_session`).
- `update_config(session_id, …, params)` — whitelist-filtered against `DEFAULT_CONFIG.keys()`; unknown keys are silently dropped so the YAML schema cannot be polluted via the network.
- `get_config_yaml(session_id, …)` — raw YAML text for the editor. If `config.yaml` does not yet exist on disk, returns a commented-defaults dump that re-persists on first Save.
- `update_config_yaml(session_id, …, yaml_text)` — parse → mapping check → whitelist filter → `update_config`. Invalid YAML or non-mapping roots raise `ValueError` (surfaced as HTTP 400 by `ui/web/app.py`).

## `models_service` — catalog

`get_models_catalog()` returns the data consumed by `PUT /api/sessions/{id}/config` / the form editor dropdown. Each provider entry carries `supports_thinking`, `thinking_style` (`budget`/`effort`/`extra_body`/`None`), `supported_efforts` (provider-specific effort vocabulary — `xhigh` is Codex-only), `default_model`, and a curated `models` list. The catalog is hand-curated: the point is to exactly mirror what the CLI registry exposes, not to scrape provider APIs at request time.

## v2.0.13 — Sub-agent surface

- `sessions_service.get_session()` / `list_sessions()` now return three
  optional fields read from `manifest.json`: `parent_session_id` (for the
  sidebar's parent → child indent grouping), `mode` (for the mode chip
  rendered next to the session name), and `display_name` (v2.0.22 — the
  human-readable label shown by the sidebar / header / sub_agent panel
  card in place of the raw `session_id`). Fields are `None` when the
  manifest doesn't set them, preserving backwards compat for any client
  that ignores unknown keys.
- `sessions_service.create_session(display_name=…)` (v2.0.22) runs the
  input through `session_init._normalize_display_name` (trim + cap at
  40 chars + non-string → `None`) and **returns the normalized value** —
  so the `POST /api/sessions` response's `display_name` always mirrors
  what was persisted to the manifest. Cap logic lives in one place to
  prevent drift between create-reply and later `GET /api/sessions` reads
  (PR #37 review finding #1).
- `POST /api/sessions` (v2.0.22) rejects `id` in the body with HTTP 400;
  `session_id` is always server-generated. See `ui/web/app.py::create_session`.
- `ui/web/app.py` adds `GET /api/sessions/{id}/events_tail?n=N` which
  returns the last ``n`` events from the session's `events.jsonl` as a
  JSON list. Used by the parent's panel card to render the last 5
  events of the sub-agent child on expand; clamped to `1 ≤ n ≤ 100`.
  Returns `[]` if the session has no events yet (rather than 404).
- `hud_service.get_hud()` payload now includes ``sub_agents_running`` —
  derived from on-disk `core/panel/*.json` entries with
  `type == TYPE_SUB_AGENT` and a non-terminal status. Lets the chat HUD
  restore the badge after a page refresh, since the SSE stream only
  re-broadcasts ``sub_agent_count`` on state change (PR #28 round 2
  Gap #7).
