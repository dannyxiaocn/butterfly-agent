# Runtime — Implementation

## Files

| File | Purpose |
|------|---------|
| `server.py` | `nutshell-server` entrypoint; auto-daemonizes with PID file, provides `start`/`stop`/`status`/`update` subcommands |
| `watcher.py` | Polls `_sessions/` for manifests, starts/stops session asyncio tasks |
| `ipc.py` | `FileIPC` — file-based IPC using two JSONL files per session |
| `bridge.py` | `BridgeSession` — frontend-friendly wrapper with dedup (wraps FileIPC) |
| `git_coordinator.py` | Master/sub role assignment for shared git repos |
| `env.py` | Best-effort `.env` loader |

## Server Lifecycle

`nutshell-server` auto-daemonizes by default. The daemon's PID is written to `_sessions/server.pid`; logs go to `_sessions/server.log`.

| Command | Behavior |
|---------|----------|
| `nutshell-server` / `nutshell-server start` | Start server in daemon mode (auto-backgrounds) |
| `nutshell-server --foreground` | Run in foreground (no daemonize) |
| `nutshell-server stop` | Send SIGTERM, wait up to 10s, then SIGKILL |
| `nutshell-server status` | Report running/stopped + PID |
| `nutshell-server update` | Stop → `pip install -e .` → restart |

All flags (`--foreground`, `--sessions-dir`, `--system-sessions-dir`) work at top level and on every subcommand via a shared parent parser.

PID file helpers (`_write_pid`, `_read_pid`, `_clear_pid`, `_is_server_running`) are parametric — they accept `system_dir` to support custom paths.

`nutshell chat` and `nutshell new` auto-start the server if it is not already running.

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
