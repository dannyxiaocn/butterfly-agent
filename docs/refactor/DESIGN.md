# Butterfly Web UI Refactor — Design

**Status**: Scaffolding doc for the `web-ui-refactor` branch. Referenced by every phase. Will be decomposed into `docs/butterfly/runtime/events.md`, `docs/butterfly/runtime/io.md`, `docs/butterfly/session_engine/design.md`, `docs/ui/web/design.md`, `docs/ui/cli/design.md` in Phase 10, then deleted.

**Owner**: `web-ui-refactor` branch. Do not reference from `main`.

---

## 1. Goals

1. **Single source of truth** for every session: `events.jsonl` records every operation (agent, user, system) in chronological order. Everything else is a view over it.
2. **LLM context is a deterministic subset** of events. One function — `build_llm_context(events)` — filters + transforms. No other code path computes LLM messages.
3. **Live and history replay are the same code path.** Writing an event and reading an event use the same primitives. Live just happens to be the tail.
4. **One IO module** (`butterfly.runtime.io`) exposes every read and every write. CLI and web both call it. No file access anywhere else.
5. **Every write is CLI-callable**, so every write is testable from a shell script without a browser.
6. **Reliability and consistency first, performance later.** No optimizations that drop information.

---

## 2. Non-negotiable invariants

- I1. `events.jsonl` is append-only. Events are never deleted, reordered, or edited.
- I2. Every agent-side or user-side operation MUST be persisted as an event with `for_llm=True` before the LLM is next called.
- I3. The LLM context passed to `provider.complete()` is exactly `build_llm_context(read_events(session_id))` — no other source.
- I4. `build_llm_context` is pure: same events in → same messages out.
- I5. The web UI never writes to any session file. All writes go through `butterfly.runtime.io`.
- I6. Every function in `butterfly.runtime.io` is exercisable from the CLI.
- I7. Live SSE and history replay emit the exact same event payloads for the same event.
- I8. Event ids are monotonically increasing within a session, so a cursor is a single integer.

Any violation is a bug. No exceptions.

---

## 3. Event schema

### 3.1 Common envelope

Every line in `events.jsonl` is a JSON object with:

```
{
  "id":      <int, monotonic per session, starting at 1>,
  "ts":      <float, seconds since epoch, with microseconds>,
  "type":    <string, see taxonomy below>,
  "for_llm": <bool, whether this event contributes to LLM context>,
  "payload": <object, type-specific fields>
}
```

No other top-level keys. No nested "meta" bag. Type-specific data goes in `payload`.

### 3.2 Event taxonomy

Events fall into three roles:

| Role | `for_llm` | Purpose |
|---|---|---|
| **user-side** | `True` | Input to the LLM from the operator or a task/sub-agent trigger |
| **agent-side** | `True` | Output from the LLM (text, thinking, tool calls) and tool execution results |
| **system-side** | `False` | Lifecycle, UI hints, diagnostics — never enters LLM context |

### 3.3 User-side events (for_llm=True)

| `type` | `payload` | When |
|---|---|---|
| `user_input` | `{ text: str, source: "cli"\|"web"\|"task"\|"sub_agent", caller: str\|None, display_name: str\|None }` | Operator or task or parent agent provides input. `source="task"` means a task card triggered this; `caller` carries the card name. `source="sub_agent"` means a sub-agent tool invocation from a parent session; `caller` is the tool name / display name. |
| `user_interrupt` | `{ text: str\|None }` | Operator pressed ⚡. If `text` is set, it becomes a user turn with that message; if `None`, just cancels the in-flight tick. Always appears in LLM context if `text` is set, because the LLM should know the user interrupted with a message. |

### 3.4 Agent-side events (for_llm=True)

| `type` | `payload` | When |
|---|---|---|
| `agent_text` | `{ text: str, model: str }` | Assistant text block. Emitted ONCE when the LLM finishes producing the text (no partial_text — see §7.2). |
| `agent_thinking` | `{ text: str, signature: str\|None, summary: str\|None, redacted: bool, interrupted: bool, reasoning_tokens: int\|None, duration_ms: float\|None }` | Assistant reasoning block. One event per block, emitted when the block closes (or when interrupted). |
| `agent_tool_call` | `{ tool_use_id: str, tool_name: str, args: object }` | LLM requested tool execution. One event per tool call. |
| `agent_tool_result` | `{ tool_use_id: str, tool_name: str, result: str, is_error: bool, is_background: bool, duration_ms: float }` | Tool finished executing. For background tools this fires when the bg task completes; the UI shows "running" between the call and this event. |

### 3.5 System-side events (for_llm=False)

| `type` | `payload` | When |
|---|---|---|
| `session_created` | `{ manifest: object }` | Session directory initialized |
| `session_started` | `{ }` | Daemon started / resumed |
| `session_stopped` | `{ reason: str }` | Daemon stopped / paused |
| `session_deleted` | `{ }` | Session scheduled for deletion |
| `model_status` | `{ status: "running"\|"idle", model: str\|None }` | UI heartbeat |
| `llm_call_usage` | `{ iteration: int, usage: object, context_tokens: int, toks_per_s: float, duration_ms: float }` | One per `provider.complete()` |
| `tool_progress` | `{ tool_use_id: str, text: str, kind: "stdout"\|"status" }` | Background tool emits intermediate progress (not for LLM) |
| `task_card_changed` | `{ name: str, card: object }` | Task card file written |
| `task_script_check` | `{ name: str, action: "skip"\|"start"\|"done", output: str }` | Task script polled |
| `task_script_error` | `{ name: str, error: str }` | Task script failed |
| `task_finished` | `{ name: str, reason: str }` | Task card reached terminal state |
| `todo_list_changed` | `{ todo_list: object }` | Todo list updated |
| `terminal_log` | `{ entry: object }` | Terminal emitted a log line |
| `terminal_state` | `{ state: object }` | Terminal state transitioned |
| `terminal_input` | `{ text: str, source: "web"\|"cli" }` | Operator entered text into terminal |
| `panel_entry_changed` | `{ entry: object }` | Panel entry updated (bg task / sub-agent) |
| `sub_agent_count` | `{ count: int }` | UI counter hint |
| `config_changed` | `{ key: str }` | `config.yaml` updated via IO |
| `prompt_changed` | `{ name: str }` | A prompt file updated via IO |
| `asset_changed` | `{ name: str }` | An asset file (tools.md/skills.md) updated via IO |
| `system_notice` | `{ text: str, level: "info"\|"warn"\|"error" }` | Generic notice line |
| `error` | `{ text: str, context: str }` | Runtime error surfaced to UI |
| `control_interrupt` | `{ text: str\|None }` | Ephemeral control request (daemon reads, acts, emits `user_interrupt` if `text` set) |
| `control_start` | `{ }` | Request daemon to start/resume |
| `control_stop` | `{ }` | Request daemon to stop |

### 3.6 Deliberate non-events

We explicitly do NOT emit these event types that existed before:

- `partial_text` — text streams in full as `agent_text` once (§7.2)
- `agent_output_start` / `agent_output_done` — duration is on `agent_text` via `llm_call_usage` pairing
- `thinking_start` / `thinking_done` / `thinking_tokens_update` — thinking is one event once closed
- `tool_done` / `tool_finalize` — collapsed into `agent_tool_result`
- `task_wakeup` — folded into `user_input` with `source="task"`
- `iteration_usage` — merged into `llm_call_usage`
- `agent_output_durations` / `agent_output_usages` / `per_iteration_usages` on turn — not needed; usage is per event

This is the single biggest simplification of the refactor.

---

## 4. LLM context builder

### 4.1 Contract

```python
def build_llm_context(events: Iterable[Event]) -> list[Message]:
    """Transform a chronological event stream into provider-ready messages.

    Invariants:
      - Pure function.
      - Only events with for_llm=True contribute.
      - Order of messages preserves event order.
      - Consecutive same-role events merge into one message with multiple content blocks.
    """
```

A `Message` is an object compatible with the provider adapter layer:

```
{ role: "user"|"assistant", content: [Block, ...] }
```

A `Block` is one of:
```
{ type: "text",        text: str }
{ type: "thinking",    text: str, signature: str|None, summary: str|None, redacted: bool }
{ type: "tool_use",    id: str, name: str, input: object }
{ type: "tool_result", tool_use_id: str, content: str, is_error: bool }
```

### 4.2 Role mapping

| Event | Role | Block |
|---|---|---|
| `user_input` | user | `text` |
| `user_interrupt` (text set) | user | `text` |
| `agent_text` | assistant | `text` |
| `agent_thinking` | assistant | `thinking` |
| `agent_tool_call` | assistant | `tool_use` |
| `agent_tool_result` | user | `tool_result` |

### 4.3 Grouping

Adjacent events of the same role merge into one message. When the role switches, a new message starts. Example sequence and resulting messages:

```
user_input          → msg[0] user  [text]
agent_thinking      → msg[1] asst  [thinking]
agent_text          → msg[1] asst  [thinking, text]
agent_tool_call×2   → msg[1] asst  [thinking, text, tool_use, tool_use]
agent_tool_result×2 → msg[2] user  [tool_result, tool_result]
agent_text          → msg[3] asst  [text]
```

Matches current Anthropic/OpenAI tool-call conventions exactly.

### 4.4 Interrupted turns

If a turn is interrupted mid-assistant (say after one `agent_tool_call` fired but before results), the event stream may end on assistant. That's fine — the next turn starts with `user_input` or `user_interrupt`. Providers accept partial assistant turns as prior context.

An interrupted `agent_thinking` still emits its event with `interrupted=True` and whatever text was captured.

### 4.5 No truncation at this layer

`build_llm_context` does not summarize, compact, or truncate. If the context exceeds the model window, the provider layer rejects it and the operator sees an error. Context-compaction is a later concern, implemented as a separate function that consumes events and yields a new (summarized) event stream, preserving the invariant that what the LLM sees is a subset of events.

---

## 5. Runtime IO API

File: `butterfly/runtime/io.py`. Single module. Every public function below.

### 5.1 Session lifecycle

```python
create_session(session_id: str, agent: str = "default", display_name: str | None = None, init_from: str | None = None) -> SessionInfo
delete_session(session_id: str) -> None
start_session(session_id: str) -> None       # emits control_start; daemon picks up
stop_session(session_id: str, reason: str = "user") -> None
list_sessions(include_archived: bool = False) -> list[SessionInfo]
get_session(session_id: str) -> SessionInfo
get_status(session_id: str) -> SessionStatus
```

### 5.2 Events (primitives)

```python
append_event(session_id: str, event_type: str, payload: dict, for_llm: bool | None = None) -> Event
read_events(session_id: str, since_id: int | None = None, until_id: int | None = None, types: Iterable[str] | None = None) -> Iterator[Event]
tail_events(session_id: str, cursor: int | None = None, timeout: float = 30.0) -> AsyncIterator[Event]
latest_event_id(session_id: str) -> int
```

`for_llm` defaults to the taxonomy in §3. Callers never need to pass it; the module knows from the event type.

### 5.3 Input (the three ways user content enters)

```python
send_message(session_id: str, text: str, source: str = "cli", caller: str | None = None, display_name: str | None = None) -> Event
interrupt_session(session_id: str, text: str | None = None) -> Event   # emits control_interrupt
```

### 5.4 Derived reads

```python
read_llm_context(session_id: str) -> list[Message]           # calls build_llm_context(read_events(...))
read_display_history(session_id: str, since_id: int = 0) -> list[Event]  # same events, suitable for UI
read_hud(session_id: str) -> HudSnapshot
read_task_cards(session_id: str) -> list[TaskCard]
read_todo_list(session_id: str) -> TodoList
read_config(session_id: str) -> dict
read_prompt(session_id: str, name: str) -> str
read_asset(session_id: str, name: str) -> str
read_panel(session_id: str) -> list[PanelEntry]
read_panel_entry(session_id: str, tid: str) -> PanelEntry
read_terminal_state(session_id: str) -> TerminalState
read_terminal_log(session_id: str, since: int = 0) -> list[TerminalLogEntry]
list_models() -> list[ModelSpec]
list_agents() -> list[AgentSpec]
```

### 5.5 Writes (all produce events)

```python
upsert_task(session_id: str, name: str, *, description: str | None = None, script: str | None = None, check_interval: float | None = None, notes: str | None = None, progress: str | None = None) -> Event
delete_task(session_id: str, name: str) -> Event
kill_panel_entry(session_id: str, tid: str) -> Event
terminal_input(session_id: str, text: str, source: str = "web") -> Event
terminal_interrupt(session_id: str) -> Event
update_config(session_id: str, key: str, value: Any) -> Event
update_prompt(session_id: str, name: str, content: str) -> Event
update_asset(session_id: str, name: str, content: str) -> Event
upsert_todo_list(session_id: str, todo_list: dict) -> Event
```

### 5.6 Write semantics

Every writer:
1. Validates inputs. Raises `IOError` with a clear message on invalid input.
2. Acquires `fcntl.flock(events.jsonl, LOCK_EX)`.
3. Reads `latest_event_id(session_id)` to compute the new id.
4. Writes the event, flushes, fsyncs.
5. Releases the lock.
6. If the event has side-effects on other files (e.g. `upsert_task` must also write `tasks/<name>.json`), the side-effect happens AFTER the event is persisted. Order: event first, then derived files. This way, the event log is always the leading truth.

### 5.7 Writer-derived state

Files like `tasks/<name>.json`, `config.yaml`, `todo_list.json` are materialized views of the event log. They exist for fast random access (e.g. "list all task cards") but the event log can regenerate them. A `rebuild_views(session_id)` function reconstructs them from events. Used in tests and during migration.

---

## 6. CLI surface

File: `butterfly/ui/cli/` entry points wired to `runtime.io`.

### 6.1 Generic reflection command

```
butterfly io <function_name> [--arg=value ...] [--json]
```

Reflects into `butterfly.runtime.io.<function_name>`. Args parsed as JSON if they look like structured data, else as strings. Output is JSON on `--json`, else a human-readable rendering.

Examples:
```
butterfly io list_sessions --json
butterfly io read_llm_context session_abc123 --json
butterfly io send_message session_abc123 --text="hello"
butterfly io upsert_task session_abc123 --name=test --description="hi"
```

Every IO function is automatically reachable. No extra wiring per function.

### 6.2 Friendly aliases

Shortcuts for common ops that wrap `butterfly io ...`:

```
butterfly chat <session_id> <text>                  # send_message + tail events
butterfly new [session_id] [--agent=NAME]           # create_session
butterfly stop <session_id>
butterfly start <session_id>
butterfly interrupt <session_id> [--text=TEXT]
butterfly delete <session_id>
butterfly sessions                                  # list_sessions (pretty)
butterfly log <session_id> [-n N] [--since=ID] [--watch]   # read_events / tail_events
butterfly tasks <session_id>                        # read_task_cards (pretty)
butterfly task-upsert <session_id> --name=...
butterfly task-delete <session_id> <name>
butterfly shell <session_id> <text>                 # terminal_input
butterfly config-set <session_id> <key> <value>
butterfly prompt-edit <session_id> <name>           # open $EDITOR
```

All aliases MUST be pure wrappers over `runtime.io.*` — no direct file IO.

### 6.3 Server commands (unchanged)

`butterfly server [tail|status|stop]` — for launching/managing the web server. Retained as-is.

---

## 7. Web & SSE protocol

### 7.1 HTTP routes

File: `ui/web/app.py`. Every route is a 3-5 line shell that:
1. Parses the request.
2. Calls exactly one `butterfly.runtime.io.*` function.
3. Returns its result as JSON.

No business logic, no error massaging beyond `try: io.foo(); except IOError as e: raise HTTPException(400, str(e))`.

Route map:
```
GET    /api/sessions                         → io.list_sessions
POST   /api/sessions                         → io.create_session
GET    /api/sessions/{id}                    → io.get_session
DELETE /api/sessions/{id}                    → io.delete_session
POST   /api/sessions/{id}/start              → io.start_session
POST   /api/sessions/{id}/stop               → io.stop_session
POST   /api/sessions/{id}/messages           → io.send_message
POST   /api/sessions/{id}/interrupt          → io.interrupt_session
GET    /api/sessions/{id}/events?since=...   → io.read_events  (JSON array, for replay)
GET    /api/sessions/{id}/events/stream      → io.tail_events  (SSE, for live)
GET    /api/sessions/{id}/hud                → io.read_hud
GET    /api/sessions/{id}/tasks              → io.read_task_cards
PUT    /api/sessions/{id}/tasks              → io.upsert_task
DELETE /api/sessions/{id}/tasks/{name}       → io.delete_task
GET    /api/sessions/{id}/todo_list          → io.read_todo_list
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

No other routes. If a feature needs a new route, it needs a new `runtime.io` function first.

### 7.2 SSE stream

```
GET /api/sessions/{id}/events/stream?cursor=<int>
```

- Emits one SSE event per `Event` in events.jsonl with id > cursor.
- SSE event `id:` field = Event id (so browser's `EventSource` reconnect resumes via `Last-Event-ID`).
- SSE event `event:` field = Event `type`.
- SSE event `data:` field = full Event object as JSON.
- One Event = one SSE event. Never two events in one SSE payload. Never a partial event.
- On reconnect, client sends `Last-Event-ID: N`; server resumes from N+1.

No partial text streaming. No mid-tool updates (except via `tool_progress` events which are their own SSE events). This is the whole point of §3.6.

### 7.3 History replay

`GET /api/sessions/{id}/events?since=0` returns the entire event list as JSON array. Frontend feeds it through the exact same reducer as the SSE stream. **History = SSE, minus the tail.**

---

## 8. Frontend contract

File: `ui/web/frontend/src/`.

### 8.1 Structural rule

**One event → one Card.** Render function is pure:

```typescript
function renderCard(event: Event): HTMLElement
```

No cross-event state. No in-place promotion. No "running" placeholder that later upgrades. The UI updates when a new event arrives and replaces / appends a card.

Exceptions — these are unavoidable minimal cross-event rules:

- **tool_use_id pairing**: an `agent_tool_call` without a matching `agent_tool_result` renders as "running"; when the result arrives, the card re-renders with the result body. Implementation: a `Map<tool_use_id, card>` lookup, no other state.
- **tool_progress**: appended to the same tool card's progress log by `tool_use_id`. Same Map lookup.

That's it. Two cross-event rules, both keyed by `tool_use_id`.

### 8.2 Unified Card component

```typescript
interface CardProps {
  kind: 'user' | 'agent-text' | 'agent-thinking' | 'agent-tool' | 'task' | 'system' | 'error';
  accent: string;           // CSS color for the left border / chip
  title: string;            // shown in the header (e.g. "You", "Thinking", "bash")
  body: string | HTMLElement;
  footer?: string;          // ts + duration + usage + any extra info
  collapsed?: boolean;      // default true for some kinds, false for others
}
```

One component. Every event type maps to a `CardProps` via a small pure function `eventToCardProps(event)`. Styling varies only by `kind` (a CSS class) and `accent`.

### 8.3 State

Minimal store:
```typescript
interface Store {
  currentSession: string | null;
  sessions: SessionInfo[];
  cards: Card[];                       // ordered by event id
  cardByToolUseId: Map<string, Card>;  // for pairing
  hud: HudSnapshot;
  tasks: TaskCard[];
  todoList: TodoList;
  panel: PanelEntry[];
  terminal: TerminalState;
}
```

Each SSE event dispatches a small reducer step. History replay dispatches the same reducer against the historical event list. No divergence path.

### 8.4 Removed components / logic

- `chat.ts` streaming bubble promotion (obsolete: no streaming)
- `chat.ts` `bashRunningCount` DOM recount (obsolete: store-derived)
- `chat.ts` `backgroundCells` Map with three-phase transitions (obsolete: unified Card)
- `chat.ts` `iteration_usage` footer late-insert (obsolete: footer set on Card creation)
- `chat.ts` `markRunningToolsInterrupted` / `markRunningThinkingInterrupted` (obsolete: events carry `interrupted` flag)
- `panel.ts` `expandedTasks` / `expandedPanel` / `todoListCollapsed` Sets (obsolete: CSS-only)
- `panel.ts` `assetCache` (obsolete: reader always hits backend)
- `diff.ts` (obsolete: backend computes diffs and attaches them to `agent_tool_call` payloads)
- `shellHighlight.ts` (retained for now — no replacement)
- `taskEditor.ts` (retained — plain form)

### 8.5 Target LOC

- `ui/web/frontend/src/*.ts` — aim < 3000 LOC (currently 8268)
- `ui/web/frontend/src/*.css` — aim < 1500 LOC (currently 2935)

---

## 9. Daemon / session engine

File: `butterfly/session_engine/session.py`.

### 9.1 Run loop

```
loop forever:
    wait for (new events on control channel) or (inactivity deadline)
    if control_interrupt: cancel in-flight tick
    else if control_stop: emit session_stopped, exit
    else:
        tick_pending = read_events(since=last_processed_id, types=["user_input", "user_interrupt", "control_*"])
        if there is a user_input / user_interrupt_with_text:
            merge all consecutive user_inputs since last tick (subject to interrupt rules §9.3)
            build_llm_context(read_events(session_id))   # full context, no accumulator
            provider.complete(context)
            stream events via append_event: agent_thinking / agent_text / agent_tool_call
            for each tool call: execute, append_event agent_tool_result
            loop until no pending tool calls
        else if task script returned [start]:
            append_event user_input(source="task", caller=name, text=prompt)
            (next iteration picks it up as above)
```

Agent state in memory is now trivial: just the asyncio task handle and the in-flight tool futures. The authoritative state is always on disk.

### 9.2 No `_history` accumulator

`Agent.run()` is rewritten to take the LLM context as an explicit argument each call. No internal history list. No `on_chunk` / `on_tool_call` / `on_thinking_end` callbacks that mutate Session state — instead the daemon subscribes to the provider's event stream and calls `append_event` directly.

### 9.3 Interrupt semantics

Two-queue dispatcher (v2.0.26) replaced by a simpler rule:
- `control_interrupt` cancels the in-flight asyncio task immediately.
- Any `user_input` events appended during or after the interrupt are simply read on the next iteration — they're in order, the daemon reads in order, done.
- `user_interrupt` with `text` is just a `user_input`-like event that interrupts first. The event itself enters LLM context as a user turn (§3.3).

This collapses the dual-queue design.

### 9.4 Tools

`butterfly/tool_engine/*` largely unchanged, except:
- Tool executors no longer fire `tool_done` / `tool_finalize` / `tool_progress_*` callbacks to Session. They directly call `append_event` with the appropriate type.
- Background tool manager also writes `tool_progress` events directly.
- Sub-agent tool: when the child session emits an `agent_text` event, the sub-agent tool appends an `agent_tool_result` to the parent. No change to the cascade cancel semantics.

### 9.5 Core files (prompts, tools, skills, config)

Kept as today: `_load_session_capabilities()` re-reads from disk each tick. This is already correct (per agent 1 audit). No change.

For history replay: the current limitation that replay does NOT re-read prompts is kept. Replay shows the historical conversation; it doesn't re-run the agent against new prompts. This is the intended behavior and matches user expectation.

---

## 10. Migration

### 10.1 Existing sessions

Sessions under `_sessions/` and `_archived/` written in the old format are converted by a one-time script `scripts/migrate_to_events_v1.py`:

1. Read old `context.jsonl` + `events.jsonl`.
2. For each entry, emit one or more new-format events.
3. Assign new monotonic ids.
4. Rewrite `events.jsonl` in place; move old files to `legacy/` within the session dir.

Mapping:
- Old `user_input` → new `user_input`
- Old `turn` → split into `agent_thinking` + `agent_text` + `agent_tool_call`* + `agent_tool_result`* (the tool_results were already inside the next turn's `messages`; extract them too)
- Old `tool_done` event → discarded (info now on `agent_tool_result`)
- Old `partial_text` → discarded (already have final text in `agent_text`)
- Old `task_wakeup` → `user_input(source="task", caller=name)`
- etc.

Migration is run once per session on first open in the new code. A marker file `events.format=v1` indicates completion.

### 10.2 Backward compatibility

None. The old format is dead after migration. No compat shim.

---

## 11. Testing strategy

### 11.1 Invariant tests

- Event schema: every event written by `append_event` conforms to the schema for its type.
- Event ids: monotonic, no gaps, no duplicates (flock-protected).
- `for_llm` flag: every event's flag matches §3 taxonomy.
- `build_llm_context` purity: same input → same output, N times.

### 11.2 Completeness tests

For a fixed session fixture covering every event type:
- Every `user_input`, `user_interrupt(text=...)`, `agent_text`, `agent_thinking`, `agent_tool_call`, `agent_tool_result` appears in the LLM context.
- No `system_*` event appears in the LLM context.
- Order preserved.
- Grouping correct (adjacent same-role merge).

### 11.3 Alignment tests

The money tests:
- Run a live session: send message, let agent respond with tool use, wait for completion. Capture the SSE event stream.
- Separately: replay the same session via `GET /api/sessions/{id}/events`. Get the event list.
- Assert: the two event streams are identical (modulo order-independent metadata).
- Assert: the same frontend reducer over both streams produces the same Card list.

### 11.4 IO contract tests

One test file per `runtime.io` function. Covers:
- Happy path
- Invalid inputs
- Concurrent writers (flock sanity)
- Event emission side-effects (upsert_task → task_card_changed event + file write)

### 11.5 End-to-end tests

A small pytest fixture: spin up a dummy provider, run `butterfly chat <id> "hi"`, let agent emit one tool call + result, assert (a) events.jsonl has the right events, (b) `GET /api/sessions/{id}/events` returns them, (c) a headless frontend rendered result matches a snapshot.

### 11.6 What we delete

- All `test_v*.py` regression pins whose underlying bugs are structurally impossible in the new design.
- All tests on `Session._current_turn_*` accumulators (gone).
- All tests on `partial_text` / `tool_done` / `tool_finalize` / `thinking_start` event shapes (gone).
- All tests on `_context_event_to_display` transformer (gone — no transformer needed).
- Mock-heavy bootstrap tests replaced with small real-path integration tests.

---

## 12. Non-goals (for this refactor)

Explicitly out of scope; tracked as future work:

- Memory-cached event reader (for performance). Today: read from disk every time.
- Context compaction (when LLM context exceeds window).
- Partial text streaming / typewriter UX.
- In-memory `_history` in Agent (re-introduce later for perf once IO is proven).
- Config editor in web (the `PUT /config` and `/prompts` / `/assets` routes stay, but frontend drops the UI in Phase 8 — power users edit via CLI).
- New agent features.

---

## 13. Phase plan (reference)

| Phase | Scope | Output |
|---|---|---|
| 0 | This doc | `docs/refactor/DESIGN.md` |
| 1 | Event primitives | `butterfly/runtime/events.py` + tests |
| 2 | LLM context builder | `butterfly/runtime/llm_context.py` + tests |
| 3 | Session engine migration | `butterfly/session_engine/session.py` rewritten |
| 4 | Reader IO surface | `butterfly/runtime/io.py` read-half |
| 5 | Writer IO surface | `butterfly/runtime/io.py` write-half |
| 6 | CLI | `butterfly/ui/cli/*` rewritten |
| 7 | Web backend | `ui/web/app.py` rewritten |
| 8 | Frontend | `ui/web/frontend/src/*` rewritten |
| 9 | Tests | `tests/*` purged + rebuilt |
| 10 | Docs | `docs/*` purged + rewritten; this file deleted |
| 11 | PR + polish | PR open, `/loop 2h` terminated |

Each phase commits standalone. `/loop 2h` fires every two hours and picks the next pending task.

---

## 14. Glossary

- **Event**: a line in `events.jsonl`.
- **Session**: a directory under `_sessions/<id>/` (live) or `_archived/<id>/` (archived).
- **LLM context**: the list of messages passed to `provider.complete()` for a given tick.
- **Tick**: one daemon iteration that produces one or more LLM calls plus any tool executions.
- **IO**: `butterfly/runtime/io.py` — the only module that touches session files.
- **Card**: a UI element representing one event.
- **Alignment**: the property that live and history-replay produce identical UI state.
