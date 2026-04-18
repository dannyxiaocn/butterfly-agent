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

## Auto-update worker (v2.0.16)

The server daemon runs an hourly background task (`_auto_update_worker` in `butterfly/runtime/server.py`) that compares local `HEAD` against `origin/main`:

- **No new commits** → delete any stale `_sessions/update_status.json` and sleep until the next tick.
- **New commits + dirty working tree** → write `update_status.json` with `{available: true, dirty: true, commits_behind: N, local_head, remote_head, checked_at}`. The web frontend polls `/api/update_status` every 30 s and shows a top-right banner; no automatic apply, because `git pull --ff-only` would clobber local work.
- **New commits + clean working tree** → run `git pull --ff-only` + `pip install -e .` + `npm run build` (best-effort; frontend failure is warned, not fatal). Then write `{applied: true, new_head, applied_at, reload: true}` and `os.execvp` the current process with fresh Python bytecode. The web frontend sees a new `applied_at` on its next poll and calls `window.location.reload()`.

Disable with `BUTTERFLY_AUTOUPDATE_INTERVAL_SEC=0`. Interval seconds default is 3600; any positive value is respected. Worker only runs when `.git/` exists (pip-only installs are skipped).

Why `os.execvp` instead of a respawn-with-parent pattern: `execvp` replaces the process image in place, so the PID is preserved, open sockets held by uvicorn in the co-located web wrapper (see `ui/cli/main.py::cmd_default`) get dropped cleanly when the wrapper itself notices the server child exited. `_clear_pid` is called immediately before `execvp` so the new image can claim the PID file unambiguously.

## Agent-output lifecycle events (v2.0.20)

Text output from an LLM call is surfaced to the frontend through three dedicated events on `events.jsonl`, all emitted from `Session` during `Agent.run()`:

| Event | Emitted by | Payload | Frontend effect |
| --- | --- | --- | --- |
| `agent_output_start` | `_make_text_chunk_callback`'s `on_chunk` on the FIRST non-empty chunk of each LLM call | `{type, ts}` | Open the "Typing…" cell immediately, without waiting for the 150-char `partial_text` flush. |
| `partial_text` | `on_chunk` when the buffer reaches 150 chars (plus one final flush after `agent.run()`) | `{type, content, ts}` | Accumulated silently into `streamingText`; used by the intermediate-finalize path when a mid-turn `tool_call` freezes the cell before an `agent` event arrives. |
| `agent_output_done` | `_make_llm_call_end_callback` when the call ends AND a start timestamp was stamped during that call | `{type, iteration, duration_ms, ts}` | Stamp `streamingEl.dataset.serverDurationMs` so the finalized "Agent 2.4s" pill matches what history replay will read back. |

`_text_output_started_at` is a Session-scoped monotonic timestamp set by the first chunk and cleared by `on_llm_call_end` (or the surrounding `finally` block in `_do_chat` / `_do_tick`) — a task cancelled between these two points MUST clear it so the next run's first LLM call doesn't inherit stale dead-time in its duration.

## Tool lifecycle — `tool_use_id` on `tool_done` (v2.0.20)

Every `tool_done` event on `events.jsonl` carries the originating `tool_use_id` (previously only `duration_ms`). `FileIPC.tail_history` makes a single pass over `events.jsonl` on each history fetch and builds a `tool_use_id → duration_ms` map; `_context_event_to_display` attaches `duration_ms` to each replayed `tool` event whose `id` matches, so reloaded tool cells render the "✓ bash 2.4s …" pill the live cell showed. The scan is keyed (not positional), so interleaved / missing entries from older sessions don't mis-associate.

## Unified CLI (v2.0.16)

`butterfly-server` and `butterfly-web` entry points were removed from `pyproject.toml`. Everything flows through the single `butterfly` command:

| Invocation | Behavior |
|------------|----------|
| `butterfly` (no args) | `_start_daemon()` backgrounds the server, then runs uvicorn in-process; prints URL; hangs. Ctrl+C stops both. |
| `butterfly server` | Tails `_sessions/server.log` via `tail -F`. Errors if server isn't running. Read-only. |
| `butterfly update` | Refuses if working tree dirty; stops server; `git pull --ff-only` + `pip install -e .` + `npm run build` (unless `--skip-frontend`); restarts server. |

Session-management subcommands (`chat`, `new`, `sessions`, `stop`, `start`, `log`, `tasks`, `panel`, `agent new`) are unchanged. `--foreground` on `python -m butterfly.runtime.server` is still the mode used by `_start_daemon` Popen and by the auto-update execvp path; the module retains `start`/`stop`/`status` subcommands for in-process use but is no longer on the user's PATH.
