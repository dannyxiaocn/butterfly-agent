# Runtime — Implementation

## Files

| File | Purpose |
|------|---------|
| `server.py` | Daemon core. Internal `start`/`stop`/`status` subcommands invoked via `python -m butterfly.runtime.server`. Hosts the auto-update worker. |
| `watcher.py` | Polls `_sessions/` for manifests, starts/stops session asyncio tasks |
| `ipc.py` | `FileIPC` — file-based IPC using two JSONL files per session |
| `bridge.py` | `BridgeSession` — frontend-friendly wrapper with dedup (wraps FileIPC) |
| `git_coordinator.py` | Master/sub role assignment for shared git repos |
| `env.py` | Best-effort `.env` loader |

## Server Lifecycle

The daemon's PID is written to `_sessions/server.pid`; logs go to `_sessions/server.log`. v2.0.16 collapsed the separate daemon console script into the unified CLI — the user-facing surface lives in `ui/cli/main.py`:

| User command | Behavior |
|--------------|----------|
| `butterfly` (no args) | Backgrounds the server (`_start_daemon`) then runs uvicorn in-process; Ctrl+C stops both. |
| `butterfly server` | Tails the running server's log. Read-only. Exits with "not running" if the daemon is down. |
| `butterfly update` | Refuses dirty tree (via `git status --porcelain`, which covers modified + staged + untracked) → stops server → `git pull --ff-only` → `pip install -e .` → `npm run build` unless `--skip-frontend` → restarts. Restores the server on any failure so the user isn't left without a daemon. |
| `butterfly chat` / `butterfly new` | Auto-start the server (via `_ensure_server_running()`) if not already up. |

Internal `python -m butterfly.runtime.server` subcommands (`start` / `stop` / `status`, plus `--foreground`) are used by `_start_daemon` Popen and by the auto-update `execvp` path. Not on the user's PATH.

PID file helpers (`_write_pid`, `_read_pid`, `_clear_pid`, `_is_server_running`) are parametric — they accept `system_dir` to support custom paths.

## Auto-update worker (v2.0.16)

`_run()` schedules `_auto_update_worker` alongside `SessionWatcher.run()` when `.git/` is present and `BUTTERFLY_AUTOUPDATE_INTERVAL_SEC > 0` (default 3600 s). Every tick:

1. `git fetch --quiet origin` (all blocking subprocess calls run via `asyncio.to_thread` so the watcher loop is not starved).
2. Compare `HEAD` vs `origin/main`. Equal → clear any stale `update_status.json`, sleep again.
3. Dirty tree (via `git status --porcelain`, which covers modified + staged + untracked) → write `update_status.json` with `{available, dirty, commits_behind, …}`. The frontend polls `/api/update_status` every 30 s and shows a top-right banner — no auto-apply, since `git pull --ff-only` would clobber local work. This detector intentionally matches `cmd_update`'s so untracked-file collisions surface as a banner rather than a silent worker failure.
4. Clean tree → `git pull --ff-only` + `pip install -e .` + `npm run build` (best-effort). Write `{applied: true, new_head, applied_at, reload: true}`. `_clear_pid()` + `os.execvp` replace the process image with one running the freshly installed code.

The watcher task and auto-update task are awaited via `asyncio.wait(FIRST_EXCEPTION)` so an unhandled error in either propagates to the `finally` block, clears the PID, and exits non-zero (no "zombie server holding the PID file" failure mode).

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

## v2.0.13 — Sub-agent / background UI events

`_runtime_event_to_display` (`butterfly/runtime/ipc.py`) forwards four
additional event types end-to-end (written by Session, consumed by the
web SSE stream):

| Type | Emitter | Carries | UI consumer |
|---|---|---|---|
| `tool_progress` | `Session._drain_background_events` on kind=`progress` | `tid`, `name`, `summary` | Chat tool-cell refreshes its "running…" meta line |
| `tool_finalize` | `Session._drain_background_events` on terminal kinds | `tid`, `name`, `kind`, `duration_ms`, `exit_code` | Chat tool-cell flips yellow→done (✓ for completed, ⚠ otherwise) |
| `sub_agent_count` | `Session._emit_sub_agent_count` | `running` (non-terminal `TYPE_SUB_AGENT` entries) | HUD "⚙ N sub-agents running" badge |
| `panel_update` | Existing, now also consumed by frontend | `tid`, `kind`, `status` | Panel tab observers |

Pair invariant: every background-spawn placeholder `tool_done` carries
`is_background=true` + `tid`, and exactly one matching `tool_finalize`
event is emitted per tid from the runner's completion. Losing one
breaks the chat cell's yellow-until-done transition, so the two are
co-tested in `tests/butterfly/tool_engine/test_pr28_review_bugs.py`.
