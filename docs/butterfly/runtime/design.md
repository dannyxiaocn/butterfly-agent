# Runtime — Design

The runtime is the **orchestration layer**. It watches session state on disk, starts daemons, and exposes file-based communication primitives.

## Responsibilities

- Discover sessions via `_sessions/<id>/manifest.json`
- Start/stop session daemon loops
- Provide file-based IPC (`context.jsonl` + `events.jsonl`)
- Expose client-side bridge for CLI and Web UIs
- Coordinate multi-agent workflows (CAP, git master/sub)

## Design Constraints

- No in-memory state that isn't backed by files
- Session ownership via PID in `status.json` — prevents duplicate daemons
- IPC is pure append-only JSONL with byte-offset polling — no sockets, no message queues

## Input dispatch — `mode` field on `user_input` (v2.0.12)

Every `user_input` event written to `context.jsonl` carries a `mode` field — `interrupt` (default) or `wait`. The runtime never inspects it; it is read by `Session._dispatch_one` to decide whether the new input cancels the in-flight run + folds (interrupt + uncommitted), preempts and runs fresh (interrupt + committed), or queues with greedy tail-merge (wait). Producers set the field as follows:

| Producer | `caller` | `source` | Default `mode` |
| --- | --- | --- | --- |
| `BridgeSession.send_message` (web/CLI/WeChat) | `human` | `user` | `interrupt` (caller may override to `wait`) |
| `BridgeSession.send_message` (programmatic agent caller) | `agent` | `user` | `interrupt` |
| `Session._drain_background_events` (BackgroundTaskManager notifications) | `system` | `panel` | `interrupt` (per spec: surface completed/stalled jobs promptly) |
| Daemon-level task wakeup enqueue | n/a (no `user_input` on disk; `TaskItem` straight to inbox) | `task` | `wait` |

`mode` is written once at producer time and never rewritten. The `BridgeSession.send_interrupt()` event remains a separate control event on `events.jsonl`; it cancels the in-flight run AND drops every queued item — distinct from a chat-with-`mode=interrupt` (which cancels and runs the new content in its place).
