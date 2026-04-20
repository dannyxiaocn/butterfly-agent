# Session Engine — Implementation

## Files

| File | Purpose |
|------|---------|
| `agent_config.py` | `AgentConfig` dataclass — reads `config.yaml`, provides typed view of manifest |
| `agent_loader.py` | `AgentLoader` — builds `Agent` from a fully self-contained agent dir |
| `agent_state.py` | Meta session lifecycle, version management, gene commands, agent→meta bootstrap |
| `session_init.py` | `init_session()` — creates full session directory structure from meta session |
| `session_config.py` | Reads/writes `core/config.yaml` with defaults |
| `session_status.py` | Reads/writes `_sessions/<id>/status.json` |
| `task_cards.py` | Per-task `.json` files in `core/tasks/` with scheduling and status management |
| `pending_inputs.py` | `ChatItem` / `TaskItem` dataclasses for the v2.0.12 input dispatcher inbox; `default_mode_for_source` and `merge_chat_content` helpers |
| `session.py` | `Session` class — wraps Agent with persistent file-backed behavior + dispatcher consumer |

## Session.run_daemon_loop() — v2.0.12 dispatcher

```
producer loop (0.5s sleep):
  ├─ _emit_version_notice_if_stale()  ← once on startup
  ├─ _drain_background_events()       ← bg-tool notifs → user_input(mode=interrupt)
  ├─ poll_interrupt()                 ← bare interrupt → cancel run + drop inbox
  ├─ poll_inputs() → for each msg:    ← reads mode field; default by source
  │     await self._enqueue(ChatItem(...))
  └─ check due task cards:
        await self._enqueue(TaskItem(card))   (if not already in _scheduled_task_names)

consumer loop (started lazily on first enqueue):
  while inbox:
    item = inbox.pop(0)
    if isinstance(item, ChatItem):
      # greedy wait-tail merge
      while inbox[0] is wait-mode ChatItem:
        item.merge_after(inbox.pop(0))
    await _dispatch_one(item)

_dispatch_one(item):
  if TaskItem: _do_tick(item.card)
  else:
    self._run_task = create_task(_do_chat(item))
    try: await self._run_task → item.resolve(result)
    except CancelledError:
      committed = len(history) > baseline
      if not committed:
        # fold cancelled content into next interrupt-mode item
        for nxt in inbox:
          if isinstance(nxt, ChatItem) and nxt.mode == 'interrupt':
            nxt.merge_before(item); break
      else:
        # partial turn already saved by _do_chat; reject caller
        item.reject(CancelledError())
```

`_do_chat` and `_do_tick` carry the prior `chat()` / `tick()` bodies; the public `chat()` / `tick()` are thin facades that build a queue item, await its future, and delegate to the dispatcher.

`Agent.run()` writes `self._history = list(messages)` after every iteration's assistant append (and again after a tool-result append) so the dispatcher can distinguish "cancelled before the LLM committed" (history unchanged → fold-merge) from "cancelled after at least one assistant turn" (history grew → save `interrupted: True` partial turn, run new chat fresh).

## init_session() Flow

1. Create `sessions/<id>/core/` + `_sessions/<id>/`
2. Write `manifest.json`, create `.venv`
3. Ensure meta session → `populate_meta_from_agent()` if first time
4. Copy prompts/tools/skills **from meta** (not directly from agent)
5. Write `config.yaml` from agent's `config.yaml`; record meta version as `agent_version`
6. Seed memory from meta → agent fallback
7. Seed playground, task cards

## AgentLoader.load()

Each agent is fully self-contained — no inheritance chain:
1. Read `config.yaml` → `AgentConfig`
2. Load prompts from paths listed under `prompts:` key
3. Load tools from paths listed under `tools:` key
4. Load skills from paths listed under `skills:` key
5. Resolve model/provider from manifest; fall back to `claude-sonnet-4-6/anthropic` if absent

## Version Management

- Meta session version: `agent_version` in `sessions/<agent>_meta/core/config.yaml`
- Child session records meta version at creation time in its own `core/config.yaml`
- `Session._emit_version_notice_if_stale()` emits a `system_notice` event if meta has advanced
- `bump_meta_version()` increments patch version and appends to history

## Session Types

| Type | Behavior |
|------|----------|
| `ephemeral` | Auto-stops after processing inputs with no pending cards |
| `default` | Standard session, no autonomous tasks |
| `persistent` | Has recurring task card (e.g. duty) with configured interval |

## Task Card System (v2.0.29 — single script per card)

Each task has two artefacts under `core/tasks/`:

- `<name>.json` — status + metadata
- `<name>.sh` — bash check polled every `check_interval` seconds while `status == pending`

JSON schema:

```json
{
  "name": "duty",
  "description": "Review and process child sessions",
  "status": "pending",
  "check_interval": 3600,
  "created_at": "2026-04-19T10:00:00",
  "last_checked_at": null,
  "last_started_at": null,
  "last_finished_at": null,
  "comments": "",
  "progress": ""
}
```

### Status values

| Status | Meaning |
|--------|---------|
| `pending` | Waiting for the next script check |
| `working` | Agent is currently running the card's wakeup |
| `finished` | Card terminal — script returned `[done]` or the agent called `task_finish` |
| `paused` | User-initiated pause; won't fire until explicitly resumed |

### Script contract

Last non-empty line of stdout decides what the runtime does:

| Tag | Meaning |
|-----|---------|
| `[skip]` | Not yet — stamp `last_checked_at`, do nothing else |
| `[start]` | Wake the agent now |
| `[start] <message>` | Wake the agent; `<message>` becomes the seed (appended to the prompt as `Trigger reason: <message>`) |
| `[done]` | Mark the card terminal — **no agent wakeup, no `agent_loop_start` hook**. Same effect as `task_finish`. |

Non-zero exit / timeout / unrecognised output → fail-closed (treated as `[skip]`) and emitted as a `task_check_error` event. Every run (success or fail) emits a `task_check` event carrying `tag`, `stdout`, `stderr`, `exit_code`, `duration_ms`. There is no `kind` field — one script means one dispatch path.

### Scheduling

The daemon's housekeeping tick (`_TASK_POLL_INTERVAL = 500 ms`) walks `core/tasks/` and:

1. For each `status=pending` card whose `needs_check(now)` is true (first check or `now - last_checked_at >= check_interval`), run `<name>.sh`. Then dispatch on `tag`:
   - `[start]` → enqueue `TaskItem(card, seed=<msg>)` into the wait queue
   - `[skip]` / fail-closed → stamp `last_checked_at` and move on
   - `[done]` → call `card.mark_terminal()` in place — the agent is **not** woken up

Cards in `working` / `finished` / `paused` are never polled. Scripts run serially and have a 10 s hard timeout (`task_runner._CHECK_TIMEOUT_SEC`); the timeout path SIGKILLs the whole process group via `os.killpg` so backgrounded children of the script (`sleep 100 &`, etc.) are reaped. All time logic (daily windows, cron-like gates, file presence) lives inside the agent-authored script — the runtime only tracks cadence.

### Wakeup invariant

The runtime emits **at most one `task_wakeup` context event and one `agent_loop_start` external hook per `[start]` poll**. `[skip]` and `[done]` are silent on those channels — they only generate `task_check` (and possibly `task_check_error`) on `events.jsonl`. This is what makes the question "did this poll wake the agent?" answerable from a single field (`task_check.tag == "[start]"`).

## Important Behaviors

- Every session gets its own `.venv` under `sessions/<id>/.venv`
- `reload_capabilities` tool is always injected at runtime
- `system_notice` events are passed through IPC and rendered in both web UI and SSE stream

## v2.0.13 — Sub-agent support

- `init_session()` grew four optional kwargs: `parent_session_id`,
  `mode` (`"explorer"` | `"executor"`), `initial_message_id`, and
  `sub_agent_depth`. All four land in `manifest.json` only when set, so
  top-level sessions keep the old manifest shape. Invalid `mode` raises
  `ValueError`; missing `toolhub/sub_agent/<mode>.md` raises
  `FileNotFoundError` rather than silently skip (cubic review, PR #28).
- `Session.__init__` reads those manifest fields, builds
  `Guardian(playground_dir)` for `mode=="explorer"`, and threads the
  guardian through `ToolLoader` into Write / Edit / Bash.
- `Session._load_session_capabilities` concatenates `core/mode.md` after
  `core/system.md` into the cached static prefix consumed by
  `Agent._build_system_parts`.
- `Session.run_daemon_loop` seeds `input_offset` via
  `_initial_input_offset()` — the byte position right after the last
  committed `turn` in `context.jsonl`, or 0 on a fresh session. This
  ensures `init_session(initial_message=...)` user_inputs are picked up
  instead of skipped (PR #28 review Bug #1).
- Session emits three new event kinds into `events.jsonl`:
  `tool_progress`, `tool_finalize`, `sub_agent_count`. They are forwarded
  through `FileIPC._runtime_event_to_display` so the SSE stream delivers
  them to the web UI unchanged.
- `Session._make_tool_done_callback` tags the placeholder `tool_done`
  emitted for background-spawn calls with `is_background=true` + `tid`;
  the UI keeps the corresponding chat cell yellow until the
  `tool_finalize` event arrives.
- `Session._emit_sub_agent_count` (+ hook from `_drain_background_events`)
  re-broadcasts the running `TYPE_SUB_AGENT` panel tally so the HUD
  badge stays accurate.
- `Session._bg_manager.register_runner("sub_agent", SubAgentRunner(...))`
  is wired in `__init__`; agent.py's `run_in_background=true` routing
  dispatches the sub_agent call through the same lifecycle plumbing as
  the bash background flow.
- `Session.__init__` now also passes `guardian=self._guardian` to
  `BackgroundTaskManager` so background-mode bash inherits the same
  boundary as inline bash (PR #28 round 2 Bug #5).
- `init_session` adds a parent-playground hand-off for sub-agent
  children: when `parent_session_id` is set, it creates the symlink
  `sessions/<child>/playground/parent → sessions/<parent>/playground/`.
  Reads work; writes through the link resolve outside the child's
  Guardian root and are denied (PR #28 round 2 Gap #6).
