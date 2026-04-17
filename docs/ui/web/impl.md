# Web UI — Implementation

## Files

| File | Purpose |
|------|---------|
| `app.py` | FastAPI app, routes, SSE stream, HUD endpoint |
| `sessions.py` | Helper functions for session metadata and initialization |
| `weixin.py` | Optional WeChat bridge |
| `frontend/` | Vite + TypeScript SPA (`npm run build` → `dist/`) |

## Key Endpoints

- `GET /api/sessions` — list sessions
- `POST /api/sessions` — create session
- `POST /api/sessions/{id}/messages` — send message
- `GET /api/sessions/{id}/events` — SSE stream with byte-offset reconnect
- `GET /api/sessions/{id}/history` — display events with offset for SSE attach
- `GET/PUT /api/sessions/{id}/tasks` — task card CRUD
- `GET/PUT /api/sessions/{id}/config` — session config
- `GET /api/sessions/{id}/hud` — HUD bar data

## Frontend Architecture

TypeScript + Vite SPA:
- `main.ts`: session attach with monotonic version token
- `sse.ts`: `SSEConnection` with contextSince/eventsSince tracking for reconnect
- `components/chat.ts`: message batching, streaming bubble management
- `components/sidebar.ts`: persistent form state

## v2.0.13 — Sub-agent UI surface

- `components/sidebar.ts` groups sessions by `parent_session_id`:
  roots render at depth 0; each root's children fan out in `created_at`
  order with `.session-item.child` styling (left border + margin) to
  imitate a markdown-list indent. An orphan child (parent not in the
  list) falls back to root so it stays reachable. A `.session-mode-chip`
  appears next to the id when `session.mode` is set.
- `components/chat.ts`:
  - HUD gains a `.hud-subagent` badge driven by `sub_agent_count` events
    (`⚙ N sub-agents running`, hidden at 0).
  - `tool_done` events now inspect `is_background` + `tid`. When present,
    the tool cell is tagged with `data-bg-tid` and stays yellow; a
    `backgroundCells: Map<tid, {el, name, startTs}>` carries the
    reference until `tool_finalize` arrives and flips it to done.
  - `tool_progress` updates the cell's summary in place (no new DOM).
  - `clearMessages()` wipes `backgroundCells` and resets the HUD badge
    so a session switch doesn't leak state.
- `components/panel.ts`:
  - New `renderSubAgentRow(entry)` branch (keyed off `entry.type === 'sub_agent'`)
    renders the child session's current activity + mode chip + tid.
  - Expanding the row lazily fetches `GET /api/sessions/{child_id}/events_tail?n=5`
    via the new `api.getEventsTail(...)` helper; the panel polling loop
    refreshes both the entry meta and the cached child events.
  - `Open child session` button calls `attachSession(childId)` to pivot
    the UI into the child.
- `sse.ts` subscription list extended with `tool_progress`,
  `tool_finalize`, `sub_agent_count`, `panel_update` so the browser
  `EventSource` actually listens for them.
- `types.ts::Session` gains optional `parent_session_id` + `mode`;
  `DisplayEvent` grows `is_background`, `tid`, `summary`, `kind`,
  `exit_code`, `running`.
