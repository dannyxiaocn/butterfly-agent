# pure_context series

Tools whose job is to ingest a raw, "dirty" byte stream (pty output with
ANSI escapes, control characters, prompt fragments) and yield **clean
text** for the LLM. Shared trait: `strip_ansi()` in
`butterfly/tool_engine/executor/pure_context/base.py`.

Current members: **`terminal_create`** + **`terminal_use`**.

## `terminal_create` + `terminal_use` — persistent pty-backed bash

One long-lived `bash --norc --noprofile` per session, attached to a
pseudo-tty. `terminal_create` opens the terminal once (returns a
4-line welcome block with `env: … / path: … / git: …` fingerprint);
`terminal_use` runs one command at a time against it. Calling
`terminal_use` before `terminal_create` fails fast with *"Error:
Terminal not created, please use terminal_create tool to create one
first"* — the two-phase design makes the boundary between "pty exists"
and "command runs" explicit, killing the single-entry-point ambiguity
that the old `session_shell(reset=true)` path exploited.

Because the stdin side is a real TTY, commands that require a terminal
— `ssh host`, `python3`, `read -p`, `ftp`, `sftp`, anything that hits
`isatty(0)` — work normally, and their state persists across tool
calls. `terminal_use("ssh host")` gets you to a remote prompt; the
next `terminal_use("ls /")` runs on the remote host; a later
`terminal_use("exit")` returns control to the local shell. The model
treats every `terminal_use` call identically — it just sends a command
and gets back output.

### Unified return rule

Every call reads output until **one** of these fires:

1. **Sentinel matches** — before the call we appended
   `printf "\n__<random>_%d__\n" $?`; a match means bash returned to its
   prompt and we captured the exit code.
2. **Output idle** — no new bytes for `idle_threshold` seconds
   (default 1.5). Covers ssh-reached-prompt, REPL-waiting-for-input,
   `read -p` parked on stdin.
3. **Total timeout** — default 60 s hard ceiling. We pass `^C` through
   the pty (which reaches the current foreground process group — local
   or remote via ssh) and wait up to 2 s for the sentinel to arrive as
   bash returns to its prompt. The shell is left alive either way; the
   agent can call `terminal_create` again on the next turn to force a
   clean respawn.

No branching on "interactive vs non-interactive" mode — one rule handles
both. Returns to the model carry `foreground=<name>` (e.g. `foreground=ssh`)
as a hint so the model knows which process owns the tty right now; this
string is cosmetic, it does **not** change return timing.

### Output hygiene

`strip_ansi()` removes CSI / OSC / short-form ESC sequences and
normalises `\r\n` → `\n`. The bash startup probe disables `echo` and
`-onlcr` on the pty so the model doesn't see its own input echoed back
or extra carriage returns. Bash's job-control notifier is turned off
(`set +o emacs / +o vi`, no `PROMPT_COMMAND`). Prompts are blanked
(`PS1=''`, `PS2=''`).

### Guardian (sub-agent explorer mode)

When a Guardian is attached the shell is pinned to `guardian.root` at
spawn time (caller can't override) and `BUTTERFLY_GUARDIAN_ROOT` is
exported into the env. Semantics unchanged from the pre-pty code.

## Terminal panel (web UI)

The pty is multi-homed: the agent calls it via the two tool verbs, and
the web user can also type into it. The Session owns one
`TerminalExecutor` — `ToolLoader` re-uses this across capability
reloads so shell state persists across ticks, and dispatches both
`terminal_create` → `.create()` and `terminal_use` → `.use()`
against the same instance.

### Files under `sessions/<id>/core/terminal/`

- `state.json` — liveness snapshot. Fields: `active`, `cwd`, `venv`,
  `git_branch`, `last_active_at`, `foreground_pid`, `foreground_cmd`,
  `locked_by`, `shell_pid`. Rewritten atomically on every state
  transition; the web panel's HUD row reads `venv / cwd / git_branch`
  straight from here.
- `log.jsonl` — append-only record of the I/O stream. Each line is a
  dict `{ts, source, text}` with `source` ∈ `{agent_cmd, agent_out,
  user_cmd, user_out, system}`. The frontend replays this on panel
  open; SSE streams the same entries as `terminal_log` events for
  live update.
- `input.jsonl` — user-typed commands queued from the web UI. Each
  line is `{ts, id, type, content?}` with `type` ∈ `{input, interrupt}`.
  Writers go through `runtime.io.terminal_input` / `terminal_interrupt`,
  which append a `terminal_input` event to `events_v1.jsonl` first and
  then enqueue the entry on `input.jsonl` for the executor to consume
  (the queue file is the effective channel today; future work: retire
  it once `TerminalExecutor` watches `events_v1` natively). The session
  daemon polls `input.jsonl` at 50 ms and forwards entries to
  `TerminalExecutor.user_input` / `.user_interrupt`. On daemon restart
  `terminal_input_offset` is seeded at the current file size so
  historical queued entries are never replayed.
- `snapshot.json` — written on idle-close (§ Idle close). Fields:
  `{ts, cwd}`.

### Locking

`TerminalExecutor._lock` is an asyncio lock. Agent calls hold it
throughout `create()` / `use()`; `locked_by_agent` is a fast-path
bool so the user-input dispatcher can cheaply reject with
`terminal_rejected` SSE events instead of queueing. Decision:
**agent wins** — while `locked_by=="agent"`, the web UI disables
the submit button and the POST `/terminal/input` route returns 409.

### HTTP surface

| Route | Purpose |
| --- | --- |
| `GET /api/sessions/{id}/terminal?tail=500` | State snapshot + last-N log entries |
| `GET /api/sessions/{id}/terminal/log?offset=N` | Resumable log tail (by byte offset) |
| `POST /api/sessions/{id}/terminal/input` body `{content}` | Queue a user command |
| `POST /api/sessions/{id}/terminal/interrupt` | Queue a Ctrl-C |

The read endpoints are unconditional file reads; the write endpoints
fast-reject (409) when `state.locked_by == "agent"`, and the daemon
re-checks on pickup (race-safe).

### SSE events

`terminal_log {ts, source, text}` per log.jsonl entry.
`terminal_state {state}` whenever state.json changes.
`terminal_rejected {id, reason}` on queued entries that were declined.

The frontend `TerminalController` keeps log buffer + input draft
outside `panel.ts`'s `innerHTML = …` churn, so panel re-renders during
high-throughput agent runs never clobber mid-typed user commands or
scroll position.

### Idle close + restore

The Session daemon calls `executor.maybe_idle_close()` each
housekeeping pass. When the pty has been idle for more than 10 minutes
(`_IDLE_CLOSE_SECONDS`), the executor probes `pwd`, writes
`snapshot.json`, and `hard_kill`s the shell. On the next call (agent or
user) `_ensure_alive()` spawns a fresh pty and, if a snapshot younger
than 7 days exists, injects `cd <cwd>` + a `[previous session ended Ns
ago — cwd restored]` system log line. Environment variables set with
`export` are **not** restored (too noisy to round-trip safely); only
cwd, which is the thing users consistently expect to come back.

### User command → events_v1.jsonl

On each accepted `user_input`:
1. Shell writes `content` + collects output until idle (1.5 s) or 30 s
   total.
2. Session daemon appends a `user_input` event to `events_v1.jsonl`
   with `source="terminal"` (caller = `terminal_user`), payload `text`
   = `"$ <cmd>\n<output>"`. `build_llm_context()` picks it up as a
   regular user-role turn on the next tick.

The agent picks the entry up at its next natural break (new LLM call)
without being preempted — consistent with how `bash(run_in_background=true)`
notifications flow.
