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

## Task Card System

Each task card is a `.json` file in `core/tasks/`:

```json
{
  "name": "duty",
  "description": "Review and process child sessions",
  "status": "pending",
  "interval": 3600,
  "start_at": "2026-04-12T11:00:00",
  "end_at": null,
  "created_at": "2026-04-12T10:00:00",
  "last_started_at": null,
  "last_finished_at": null,
  "comments": "",
  "progress": ""
}
```

`end_at: null` (default) means the card never auto-expires. Set an explicit ISO timestamp (e.g. `"2026-04-19T10:00:00"`) to create a bounded window.

### Status values

| Status | Meaning |
|--------|---------|
| `pending` | Waiting for next trigger (default state for new and recurring tasks) |
| `working` | Currently being executed |
| `finished` | Completed (one-shot) or manually finished |
| `paused` | User-initiated pause; won't fire until explicitly resumed |

### Scheduling (`start_at` / `end_at`)

- `start_at`: earliest time a task can fire. Default for recurring = `ceil(created_at + interval)`; for one-shot = `floor(created_at)`.
- `end_at`: auto-expire time. No default — `None` means the card never expires. Callers that want a bounded window set an explicit ISO timestamp.
- Hour-level granularity: `_ceil_to_hour()` rounds up, `_floor_to_hour()` truncates down.
- A task with `status=pending` fires when: `now >= start_at AND (end_at is None OR now < end_at) AND (never finished OR interval elapsed)`.
- If `end_at` is set and `now >= end_at` → auto-marked `finished` and persisted to disk by `load_due_cards()`. Cards with `end_at=None` are never auto-expired.

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

## v2.0.17 — thinking-block persistence

- `Session._make_thinking_callbacks` now returns a **4-tuple**:
  `(on_thinking_start, on_thinking_end, had_any, get_collected)`. The
  new `get_collected()` accessor returns the list of `{block_id, text,
  duration_ms, ts}` dicts captured as each `on_thinking_end` fires.
- `_do_chat` (both success + CancelledError branches), `_do_tick`,
  `_save_chat_turn`, and `_save_partial_chat_turn` persist the
  collected blocks on the turn as `thinking_blocks: [...]`. History
  replay reads that field in `FileIPC._context_event_to_display` and
  emits `thinking` display events directly, so re-entering a session
  restores the cells across every provider — including codex (whose
  reasoning items were never embedded in the Anthropic-style
  `{"type":"thinking"}` content blocks).
- A started-but-unclosed block (CancelledError mid-thinking) is **not**
  collected — `on_thinking_end` is the only append site. The on-disk
  turn therefore never references a cell the frontend can't render;
  the orphan running cell is handled by `markRunningThinkingInterrupted`
  in `chat.ts` when `model_status:idle` lands.
