# Runtime — Implementation

## Files

| File | Purpose |
|------|---------|
| `server.py` | `nutshell server` entrypoint; creates `SessionWatcher` and runs it |
| `watcher.py` | Polls `_sessions/` for manifests, starts/stops session asyncio tasks |
| `ipc.py` | `FileIPC` — file-based IPC using two JSONL files per session |
| `bridge.py` | `BridgeSession` — frontend-friendly wrapper with dedup (wraps FileIPC) |
| `cap.py` | File-backed coordination primitives: handshake, lock, broadcast, heartbeat-sync |
| `git_coordinator.py` | Master/sub role assignment for shared git repos |
| `env.py` | Best-effort `.env` loader |

## SessionWatcher

Main server loop:
1. Scans `_sessions/` every 1 second for directories with `manifest.json`
2. Skips sessions with alive PIDs or stopped status
3. Creates asyncio task → reads manifest → builds Agent → starts `session.run_daemon_loop()`

## FileIPC

Two files per session in `_sessions/<id>/`:

| File | Contents |
|------|----------|
| `context.jsonl` | `user_input` + `turn` events (conversation history) |
| `events.jsonl` | `model_status`, `partial_text`, `tool_call`, `tool_done`, `loop_start`, `loop_end`, etc. |

- Write side (daemon): `append_context()`, `append_event()`
- Read side (daemon): `poll_inputs(offset)`, `poll_interrupt(offset)`
- Read side (UI): `tail_context(offset)`, `tail_runtime_events(offset)`

## BridgeSession

Client-side handle for frontends:
- `send_message(content)` → writes `user_input` to `context.jsonl`
- `send_interrupt()` → writes `interrupt` to `events.jsonl`
- `iter_events()` → yields deduped display events from both files
- Uses `BoundedIDSet` (FIFO ring buffer, capacity 256) for dedup

## Important Behaviors

- `last_running_event_offset()`: returns byte offset of last `model_status:running` for SSE reconnect
- Thinking blocks use per-block IDs (`thinking:{ts}:{idx}`) for multi-block dedup
- Stopped sessions can auto-expire back to active after several hours
