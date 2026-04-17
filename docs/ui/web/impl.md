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

## v2.0.17 — Web UI stability fixes

- **Streaming bubble** (`components/chat.ts`): `partial_text` events carry
  deltas (~150 char flushes), not a cumulative buffer. Frontend now
  accumulates into `streamingText` and replaces `.msg-streaming-body` on
  every chunk, so the bubble grows incrementally. On the final `agent`
  event the bubble is **promoted in-place** — cursor removed, classes
  flipped, header rewritten with usage stats — instead of being destroyed
  and replaced by a fresh DOM node, which used to flash the full reply
  as a large block at the end. The old three-dot `streaming-badge` /
  `generating…` label is gone; a single `.streaming-cursor` element
  renders `Working… ▌` below the body and is removed when the turn
  finalizes.
- **Thinking cells**:
  - On `model_status:idle`, any still-open `.msg-thinking-running` cells
    are flipped to `.msg-thinking-interrupted` (red `⚠ Thinking
    interrupted · cancelled`) so a mid-thinking interrupt doesn't leave
    a spinner running forever.
  - History-replay `thinking` events are deduped against already-rendered
    cells by `data-block-id` + `data-event-id`, so a visibilitychange
    re-fetch can't paint a second copy of a block the live stream
    already rendered.
  - Replay cells carry `data-block-id` / `data-event-id`, and show
    `Thought for Xs` when the persisted `duration_ms` is available.
- **Scroll preservation** (`components/sidebar.ts`, `components/panel.ts`,
  `components/chat.ts`): `render()` now snapshots `scrollTop` before
  rewriting `innerHTML` and restores it after. Chat auto-scroll is
  gated on `isNearBottom()` (80 px leeway) so streaming updates don't
  yank the viewport when the user has scrolled up to read prior
  context.
- **New-session form state** (`components/sidebar.ts`): the 3-5 s
  session/weixin polls used to wipe the agent input back to its default
  mid-typing. The component now snapshots input values, focus, and
  selection range before each re-render and restores them afterward,
  and also stores the running value in a `formState` closure so the
  *first* paint after a poll already has the user's text.
- **HUD model label** (`butterfly/service/hud_service.py`): when
  `config.yaml.model` is null, the HUD now looks up the provider's
  `default_model` from the models catalog and displays it as
  `"<model> (provider default)"` instead of the opaque `(default)`.
- **Persisted thinking** (`butterfly/session_engine/session.py`,
  `butterfly/runtime/ipc.py`): `_make_thinking_callbacks()` returns a
  new `get_collected()` accessor; turn writers persist a
  `thinking_blocks: [{block_id, text, duration_ms, ts}]` list on each
  turn. `_context_event_to_display(for_history=True)` reads from that
  list first, falling back to the legacy message-content scan — so
  re-entering a session now shows the full thinking history for every
  provider, including codex (whose reasoning items never landed in the
  Anthropic-style `{"type":"thinking"}` content blocks).
