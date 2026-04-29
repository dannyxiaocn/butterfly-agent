# Events — `events_v1.jsonl` contract

Every session records each operation as one line in `_sessions/<id>/events_v1.jsonl`. This file is the single source of truth: LLM context, the web transcript, history replay, and the CLI log are all views over the same event stream.

## Envelope

Every line is a JSON object with exactly these keys:

```json
{
  "id":      1,
  "ts":      1713830400.123456,
  "type":    "user_input",
  "for_llm": true,
  "payload": { "...": "type-specific" }
}
```

- `id` — monotonic integer per session, starts at 1, strictly increasing (flock-protected).
- `ts` — seconds since epoch, microsecond resolution.
- `type` — one of the constants below.
- `for_llm` — whether `build_llm_context` picks up the event. Never computed at read time; baked in at append.
- `payload` — type-specific bag. Fields listed below. No nested meta bag.

No other top-level keys. Unknown types default `for_llm=False` with a one-time warning — add the type to `_FOR_LLM_DEFAULTS` in `butterfly/runtime/events.py` before emitting it.

## Roles and `for_llm` default

| Role | `for_llm` default | Purpose |
|---|---|---|
| user-side | `True` | Input the LLM should see on the next call |
| agent-side | `True` | Output produced by the LLM on a previous call |
| system-side | `False` | Lifecycle, UI hints, diagnostics |

Callers never pass `for_llm`; the event type implies it via `_FOR_LLM_DEFAULTS`.

## User-side events (for_llm=True)

| `type` | `payload` |
|---|---|
| `user_input` | `{ text: str, source: "cli"\|"web"\|"task"\|"sub_agent", caller: str?, display_name: str? }` |
| `user_interrupt` | `{ text: str? }` — when `text` is set, the LLM sees this as a user turn |

`source="task"` means a task card triggered the input; `caller` is the card name. `source="sub_agent"` means a parent session's `sub_agent` tool; `caller` is the tool's display name.

## Agent-side events (for_llm=True)

| `type` | `payload` |
|---|---|
| `agent_text` | `{ text: str, model: str }` |
| `agent_thinking` | `{ block_id: str, text: str, signature: str?, summary: str?, redacted: bool, interrupted: bool, reasoning_tokens: int?, duration_ms: float? }` |
| `agent_tool_call` | `{ tool_use_id: str, tool_name: str, args: object }` |
| `agent_tool_result` | `{ tool_use_id: str, tool_name: str, result: str, is_error: bool, is_background: bool, duration_ms: float }` |

One event per assistant text block, reasoning block, tool call, or tool result. No partial streaming; a block emits once on close. For background tools, `agent_tool_result` fires when the background job completes — the UI shows "running" between the call and the result.

## Agent-side UI lifecycle markers (for_llm=False)

These pair with the canonical agent-side events above to drive two-phase UI rendering. They never enter LLM context (`build_llm_context` filters them out).

| `type` | `payload` | When |
|---|---|---|
| `agent_thinking_start` | `{ block_id: str }` | Provider opened a thinking stream. Pairs by `block_id` with the eventual `agent_thinking` close event. The frontend renders a spinning "Thinking…" cell here and finalises it on close. |
| `agent_bg_tool_dispatched` | `{ tool_use_id: str, tool_name: str, tid: str, placeholder: str }` | A tool result returned the bg-spawn placeholder (`"Task started. task_id=…"`) so the panel will track it. The frontend keeps the cell yellow ("running") until the deferred `agent_tool_result` lands. Only emitted on the bg path; inline tools skip it. |

## System-side events (for_llm=False)

| `type` | `payload` | When |
|---|---|---|
| `session_created` | `{ manifest: object }` | Session dir initialised |
| `session_started` | `{}` | Daemon started/resumed |
| `session_stopped` | `{ reason: str }` | Daemon stopped/paused |
| `session_deleted` | `{}` | Session scheduled for deletion |
| `model_status` | `{ status: "running"\|"idle", model: str? }` | UI heartbeat |
| `llm_call_usage` | `{ iteration: int, usage: object, context_tokens: int, toks_per_s: float, duration_ms: float }` | One per `provider.complete()` |
| `tool_progress` | `{ tool_use_id: str, text: str, kind: "stdout"\|"status" }` | Background tool intermediate progress |
| `task_card_changed` | `{ name: str, card: object }` | Task card file written |
| `task_script_check` | `{ name: str, action: "skip"\|"start"\|"done", output: str }` | Task script polled |
| `task_script_error` | `{ name: str, error: str }` | Task script failed |
| `task_finished` | `{ name: str, reason: str }` | Task card reached terminal state |
| `todo_list_changed` | `{ todo_list: object }` | Todo list updated |
| `terminal_log` | `{ entry: object }` | Terminal emitted a log line |
| `terminal_state` | `{ state: object }` | Terminal state transitioned |
| `terminal_input` | `{ text: str, source: "web"\|"cli" }` | Operator entered terminal input |
| `panel_entry_changed` | `{ entry: object }` | Panel entry (bg task/sub-agent) updated |
| `sub_agent_count` | `{ count: int }` | UI counter hint |
| `config_changed` | `{ key: str }` | `config.yaml` updated via io |
| `prompt_changed` | `{ name: str }` | Prompt file updated via io |
| `asset_changed` | `{ name: str }` | Asset file (tools.md/skills.md) updated via io |
| `system_notice` | `{ text: str, level: "info"\|"warn"\|"error" }` | Generic notice line |
| `error` | `{ text: str, context: str }` | Runtime error surfaced to UI |
| `control_interrupt` | `{ text: str? }` | Ephemeral control; daemon reads, acts, emits `user_interrupt` if `text` set |
| `control_start` | `{}` | Request daemon to start/resume |
| `control_stop` | `{}` | Request daemon to stop |

## Retired events

These event types are deliberately not emitted. Earlier releases had them; the events-as-truth refactor collapsed each one:

- `partial_text` — text emits once on block close as `agent_text`.
- `agent_output_start` / `agent_output_done` — duration lives on `agent_text` via the `llm_call_usage` pairing.
- `thinking_done` / `thinking_tokens_update` — folded into `agent_thinking` (close-only). The open phase resurfaced as the `agent_thinking_start` UI marker (for_llm=False) above; reasoning never enters LLM context twice.
- `tool_done` / `tool_finalize` — folded into `agent_tool_result`. The bg-spawn yellow→green transition is now driven by the `agent_bg_tool_dispatched` UI marker paired with the canonical `agent_tool_result`.
- `task_wakeup` — folded into `user_input` with `source="task"`.
- `iteration_usage` — merged into `llm_call_usage`.

## Write semantics

`butterfly.runtime.events.append_event(session_dir, type, payload, *, for_llm=None, ts=None)`:

1. Acquire `fcntl.flock(events_v1.jsonl, LOCK_EX)`.
2. Scan the file for the last id; new id = last + 1.
3. Resolve `for_llm` via the taxonomy if not explicit.
4. Write one compact JSON line + `\n`, `fsync`.
5. Release the lock.

The event is persisted BEFORE any derived state (`tasks/<name>.json`, `config.yaml`, `todo_list.json`). Derived files are materialised views; the event log is the leading truth and can regenerate them.

## Read semantics

- `read_events(session_dir, *, since_id=None, until_id=None, types=None)` — iterator. `since_id` is exclusive, `until_id` is inclusive. Missing file yields nothing.
- `latest_event_id(session_dir)` — largest id in the file, or 0.
- `tail_events(session_dir, *, cursor=None, timeout=30.0, poll_interval=0.1)` — async iterator. Yields events with id > cursor; stops after `timeout` seconds of silence. `cursor=None` yields from the start.

All three tolerate malformed lines (skipped silently) and missing files (empty result).
