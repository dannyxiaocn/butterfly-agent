# Web UI — HTTP, SSE, and the chat transcript

The web backend is a thin shell over `butterfly.runtime.io`. Every route parses its request, calls exactly one `io.*` function, and returns the result as JSON. No business logic in `ui/web/app.py`. The frontend renders the transcript by funnelling every BfEvent through `eventAdapter.bfToDisplay()` before dispatching it to `chat.ts`'s `appendEvent` — live SSE and history replay drive the same code path.

## HTTP routes

`ui/web/app.py`. Each handler is three to five lines: parse → `io.foo(...)` → JSON. Unknown errors become 500; `FileNotFoundError` → 404; `ValueError` → 400.

```
GET    /api/sessions                         → io.list_sessions
POST   /api/sessions                         → io.create_session
GET    /api/sessions/{id}                    → io.get_session
DELETE /api/sessions/{id}                    → io.delete_session
POST   /api/sessions/{id}/start              → io.start_session
POST   /api/sessions/{id}/stop               → io.stop_session
POST   /api/sessions/{id}/messages           → io.send_message  (body.mode ∈ {interrupt, wait})
POST   /api/sessions/{id}/interrupt          → io.interrupt_session
GET    /api/sessions/{id}/events?since=...   → io.read_events            (JSON, replay)
GET    /api/sessions/{id}/history            → io.read_display_history   (JSON, replay)
GET    /api/sessions/{id}/events/stream      → io.tail_events            (SSE, live)
GET    /api/sessions/{id}/hud                → io.read_hud
GET    /api/sessions/{id}/tasks              → io.read_task_cards
PUT    /api/sessions/{id}/tasks              → io.upsert_task
DELETE /api/sessions/{id}/tasks/{name}       → io.delete_task
GET    /api/sessions/{id}/todo_list          → io.read_todo_list
POST   /api/sessions/{id}/todo_list          → io.upsert_todo_list
GET    /api/sessions/{id}/config             → io.read_config
PUT    /api/sessions/{id}/config             → io.update_config
GET    /api/sessions/{id}/prompts/{name}     → io.read_prompt
PUT    /api/sessions/{id}/prompts/{name}     → io.update_prompt
GET    /api/sessions/{id}/assets/{name}      → io.read_asset
PUT    /api/sessions/{id}/assets/{name}      → io.update_asset
GET    /api/sessions/{id}/panel              → io.read_panel
GET    /api/sessions/{id}/panel/{tid}        → io.read_panel_entry
POST   /api/sessions/{id}/panel/{tid}/kill   → io.kill_panel_entry
GET    /api/sessions/{id}/terminal           → io.read_terminal_state
GET    /api/sessions/{id}/terminal/log       → io.read_terminal_log
POST   /api/sessions/{id}/terminal/input     → io.terminal_input
POST   /api/sessions/{id}/terminal/interrupt → io.terminal_interrupt
GET    /api/models                           → io.list_models
GET    /api/agents                           → io.list_agents
```

If a feature needs a new route, it needs a new `runtime.io` function first. No routes compute anything the CLI can't.

## SSE stream

```
GET /api/sessions/{id}/events/stream?cursor=<int>
```

- One `Event` from `events_v1.jsonl` = one SSE frame.
- SSE `id:` = Event `id` (browser auto-reconnect sends `Last-Event-ID`, which the server reads and resumes from).
- SSE `event:` = Event `type`.
- SSE `data:` = full Event object as JSON.
- Keep-alive comment (`: keepalive\n\n`) every 30 s while idle.
- Server honors `Last-Event-ID` header over the `?cursor=` query param.

No partial-text streaming. No multi-event batching. Shutdown is cooperative: uvicorn's `shutdown_event` gates the generator loop so Ctrl+C unwinds cleanly.

## History replay

`GET /api/sessions/{id}/events?since=0` returns the entire event list as a JSON array. The frontend feeds it through the exact same `eventAdapter.bfToDisplay()` + `appendEvent` pipeline as the SSE stream. **History = SSE, minus the tail.** Both live and replay produce the same transcript (alignment invariant).

## Frontend event model

The frontend speaks a `DisplayEvent` shape (flat `{type, content, ...}`) rather than the BfEvent envelope on the wire. `eventAdapter.bfToDisplay()` is the single translation seam — every BfEvent crossing SSE or `/history` replay is funneled through it before any view code sees it. Replay and live stream therefore dispatch the same `appendEvent(event)` step in `chat.ts`; alignment is preserved.

The chat pane is multi-event-per-cell: a tool call + its result render as one cell that transitions states; thinking_start + thinking_done pair to flip a spinning placeholder into a finalised cell; bg-tool yellow→green is driven by the `agent_bg_tool_dispatched` marker (call → dispatched → result). Cell pairing keys off `tool_use_id` and `block_id`.

Lifecycle markers carried for_llm=False on the wire:

- `agent_thinking_start` / `agent_thinking` — paired by `block_id`. Open renders the spinning placeholder; close finalises it.
- `agent_tool_call` / `agent_bg_tool_dispatched` / `agent_tool_result` — bg path keys off `tool_use_id`; the dispatched marker keeps the cell yellow until the deferred result lands.

Inline tools skip `agent_bg_tool_dispatched`; they go straight from call → result.

## Reducer and store

The store's chat slice is an append-only event list; per-cell mutations happen inside `chat.ts`'s `appendEvent` handler, which keys off `tool_use_id` and `block_id` for the multi-event pairings above. `eventAdapter.bfToDisplay()` is responsible for shape parity between live SSE and history replay.

## Source layout

```
ui/web/app.py                          # FastAPI routes
ui/web/frontend/src/api.ts             # fetch() wrappers for the route map
ui/web/frontend/src/sse.ts             # EventSource wrapper, Last-Event-ID resume
ui/web/frontend/src/eventAdapter.ts    # BfEvent → DisplayEvent translation seam
ui/web/frontend/src/store.ts           # StoreShape + subscribe
ui/web/frontend/src/main.ts            # app entrypoint, DOM wiring
ui/web/frontend/src/components/chat.ts # transcript pane: appendEvent dispatch + cell rendering
ui/web/frontend/src/components/panel.ts# bg task / sub-agent panel
ui/web/frontend/src/components/header.ts, sidebar.ts  # HUD chrome
```

The previous unified-Card / single-reducer architecture has been rolled back in favour of restoring the pre-#57 multi-event cell pairings. The data layer (events_v1.jsonl, runtime.io) remains the new design; only the presentation seam was reverted.
