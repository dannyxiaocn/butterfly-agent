# Web UI — HTTP, SSE, and the unified Card

The web backend is a thin shell over `butterfly.runtime.io`. Every route parses its request, calls exactly one `io.*` function, and returns the result as JSON. No business logic in `ui/web/app.py`. The frontend renders one Card per event via one reducer — live SSE and history replay drive the same code path.

## HTTP routes

`ui/web/app.py`. Each handler is three to five lines: parse → `io.foo(...)` → JSON. Unknown errors become 500; `FileNotFoundError` → 404; `ValueError` → 400.

```
GET    /api/sessions                         → io.list_sessions
POST   /api/sessions                         → io.create_session
GET    /api/sessions/{id}                    → io.get_session
DELETE /api/sessions/{id}                    → io.delete_session
POST   /api/sessions/{id}/start              → io.start_session
POST   /api/sessions/{id}/stop               → io.stop_session
POST   /api/sessions/{id}/messages           → io.send_message
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

`GET /api/sessions/{id}/events?since=0` returns the entire event list as a JSON array. The frontend feeds it through the exact same reducer as the SSE stream. **History = SSE, minus the tail.** Both live and replay produce the same Card list (alignment invariant).

## Unified Card

One event → one Card. Render function is pure:

```typescript
function renderCard(props: CardProps): HTMLElement
```

`CardProps` is a small tagged union:

```typescript
interface CardProps {
  kind: 'user' | 'agent-text' | 'agent-thinking' | 'agent-tool' | 'task' | 'system' | 'error';
  accent: string;
  title: string;
  body: string | HTMLElement;
  footer?: string;
  collapsed?: boolean;
}
```

`cardPropsFor(card)` (pure) maps each event type to `CardProps`. Styling varies by `kind` (a CSS class) and `accent`.

### Two cross-event rules (both keyed by `tool_use_id`)

1. `agent_tool_call` without a matching `agent_tool_result` renders "running"; when the result arrives, the same Card re-renders with the result body.
2. `tool_progress` is appended to the same Card's progress log.

Implementation: a `Map<tool_use_id, Card>` lookup. No other cross-event state. Every other event produces an independent Card.

## Reducer and store

`reduce(event)` returns a list of card ids to re-render. `reduceMany(events)` is the replay fast path — used for history. SSE handlers call `reduce` per frame. Store shape:

```typescript
interface StoreShape {
  currentSession: string | null;
  sessions: SessionInfo[];
  cards: Card[];                       // ordered by event id
  cardByToolUseId: Map<string, Card>;  // for tool pairing
  hud: HudSnapshot;
  tasks: TaskCard[];
  todoList: TodoList;
  panel: PanelEntry[];
  terminal: TerminalState;
}
```

Live SSE events and replay events dispatch the identical reducer step. No divergence path.

## Source layout

```
ui/web/app.py                          # FastAPI routes
ui/web/frontend/src/api.ts             # fetch() wrappers for the route map
ui/web/frontend/src/sse.ts             # EventSource wrapper, Last-Event-ID resume
ui/web/frontend/src/reducer.ts         # reduce(event) → store mutation
ui/web/frontend/src/store.ts           # StoreShape + subscribe
ui/web/frontend/src/card.ts            # renderCard + cardPropsFor
ui/web/frontend/src/main.ts            # app entrypoint, DOM wiring
ui/web/frontend/src/components/        # panel, sidebar, HUD chrome
```

Previous ad-hoc state (streaming bubble promotion, `bashRunningCount` DOM recount, three-phase background-cell transitions, `markRunningToolsInterrupted`, `expandedTasks` sets, `assetCache`, the `diff.ts` module) is gone. The unified Card + one-reducer model replaces all of it.
