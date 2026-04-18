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

## v2.0.23 — UI polish pentad

### TaskCard + interrupt + error cell

- `components/panel.ts::renderTaskCard` now emits `<details class="task-card">` + `<summary class="task-card-summary">` instead of a flat div. Summary holds name + `duty` pill + status badge + compact interval pill + caret. The body (only visible when expanded) shows `window`, `last run`, full `description`, the previously-hidden `progress` and `comments` fields from `TaskCard`, and the `Edit` button (no longer opacity-0-on-hover — it lives behind the caret now). `.task-card-section-body` renders in plain flow (no quote-style `border-left`/`padding-left` indent).
- `components/chat.ts::markRunningToolsInterrupted` added — called from the `model_status: idle` branch alongside `markRunningThinkingInterrupted`. Sweeps any `.msg-tool:not(.done)` DOM node into a `done interrupted` terminal state (dim yellow chrome + `✗ interrupted Xs` pill), clears `runningTools` + `backgroundCells` maps and the HUD ▶ tool indicator. This is what makes the ⚡ Interrupt button feel responsive — the backend was already cancelling correctly (see `docs/butterfly/session_engine/design.md`), only the UI feedback loop was missing.
- `components/chat.ts` `tool_done` handler reads the new `event.is_error` flag, adds `.msg-tool.error` class, swaps the icon from ✓ to ✗. `renderToolEvent` (used by history replay) mirrors the same logic so reloaded sessions don't lose the red state.
- `types.ts::DisplayEvent` grows `is_error?: boolean`. CSS adds `.msg-tool.error` (red border + icon + duration) and `.msg-tool.interrupted` (yellow dim), both declared after the pre-existing `.msg-tool.done` rules so they win on equal specificity.

### User-input glass cards (3 variants) + task_wakeup unification

- `components/chat.ts::renderEvent` `case 'user'` rewritten as a collapsible `<details class="user-details">` chrome with frosted-glass styling (`backdrop-filter: blur(10px) saturate(1.2)` + translucent tint + coloured border). `userCellVariant(event)` pure fn classifies into one of three variants based on backend-propagated origin fields:
  - `you` (default / `caller=human` / `caller` absent): light green `#7fcf7f` — label "You"
  - `tool-output` (`caller=system` + `source=panel`): orange-yellow `#e8a94a` — label "Tool output — <tool_name>"
  - `task` (`caller=task`): sky blue `#7ac3f2` — label "Wakeup — <card>" (reserved — task runs don't currently emit `user_input` events; the `task_wakeup` path below handles the live surface)
- `components/chat.ts::renderEvent` `case 'task_wakeup'` rewritten to render the legacy `⏱ task wakeup: <card>` one-liner through the same glass-card chrome as `.msg-user-task` (sky blue) — "Wakeup" + dim card name in the summary, placeholder body. Drops the old `.msg-task-wakeup` styling entirely.
- `types.ts::DisplayEvent` grows `caller?: string` + `tool_name?: string`. `runtime/ipc.py::_context_event_to_display` for the `user_input` branch now forwards `caller`, `source`, `tid`, `kind`, `tool_name`, `card` onto the emitted `user` display event (previously only `content` + `id` + `ts` escaped). `session.py::_drain_background_events` gained `tool_name: entry.tool_name` on the bg-tool notification `user_input` event so the card's dim sub-label can render without parsing the free-form message body.
- CSS: `.msg-user` glass base (backdrop-filter, rounded border, padding) + `.user-details/.user-summary/.user-label/.user-word/.user-meta/.user-body` internal chrome. Colour variants driven by `--user-accent`/`--user-border`/`--user-bg` custom properties on `.msg-user-you/-tool-output/-task`. All three right-align at 80% max-width (preserving the pre-v2.0.23 user-side flex-end positioning). `.msg-task-wakeup` block removed; `.msg-user .msg-label` override removed (new chrome uses `.user-word`).
