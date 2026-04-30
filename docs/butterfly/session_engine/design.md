# Session engine — daemon run loop

The session engine owns one Python task per live session. It polls the session's event log for user input, calls the LLM, executes tools, and writes every step back as events. The event log is the leading truth; in-memory state is rebuilt from disk at the start of every tick.

## Events-as-truth

`events_v1.jsonl` is authoritative (invariant I3 from the refactor). `Session._rebuild_history_from_events()` runs before every `Agent.run()` call and replaces the agent's in-memory `_history` with `build_llm_context(read_events(session_dir))`. Two coercion steps run on top of the pure builder output:

1. **Selective trim** — the trailing user-role message is dropped only when it is *text-only*; a tail carrying a `tool_result` block is kept so its paired `function_call` from the prior assistant turn is not orphaned (gpt-5 rejects orphans with HTTP 400). The trim still prevents the dispatcher's currently-dispatched `user_input` from being double-counted when `Agent.run()` re-composes `[*_history, Message(role="user", content=input)]`.
2. **Role coercion** — `build_llm_context` groups `agent_tool_result` events under `role="user"` (matches Anthropic's wire shape per DESIGN.md §4), but `Agent._history`'s internal convention is `role="tool"` for tool-result-only messages. Rebuild flips the role at this seam so the OpenAI Responses provider's `_convert_messages` dispatches through `_convert_tool_result` → `function_call_output`. Anthropic's provider remaps `tool` → `user` before sending, so either input role lands on the same wire bytes.

Consequences:

- A daemon restart rehydrates exactly from disk — no resume-from-context.jsonl path.
- An interrupt that rolls back in-memory history is fine; the next tick realigns against whatever events_v1 holds.
- Drift between disk and memory is a bug. `_check_history_alignment(where)` logs a WARN when `len(on_disk_messages) != len(in_memory_history)` so regressions surface.
- Rebuild swallows exceptions: a disk hiccup MUST NOT prevent the daemon from running; the in-memory history is kept as a fallback.

## Run loop

```
loop forever:
    poll events_v1 + context.jsonl for new input
    if control_interrupt:  cancel in-flight tick
    elif control_stop:     emit session_stopped, exit
    else:
        drain pending user_input + user_interrupt(text) into one turn
        _rebuild_history_from_events()
        provider.complete(...)
            → for each block the provider yields:
                append_event(agent_text | agent_thinking | agent_tool_call)
            → for each tool call:
                execute, append_event(agent_tool_result)
        loop until no pending tool calls
    else if task script returned [start]:
        append_event(user_input, source="task", caller=name, text=seed)
        next iteration picks it up
```

Agent state in memory is trivial — the asyncio task handle, in-flight tool futures, and the ephemeral `_history` accumulator that `_rebuild_history_from_events` repopulates. Persistence lives on disk.

## Dual-emit during the transition

The Phase 3a migration dual-writes: legacy writers (`context.jsonl` turn writer, `events.jsonl` legacy-schema entries) still fire so old tests and tooling keep working; events_v1 is the new truth. Phase 3b flipped `Agent.run()` to read from events_v1 via `_rebuild_history_from_events`. Future phases collapse the legacy writers — search for "Phase 3a dual-write" in `session.py` to find the remaining sites.

## Input dispatch

The dispatcher keeps two queues (`_interrupt_queue`, `_wait_queue`) inherited from v2.0.24. The events-as-truth refactor simplified the semantics:

- `control_interrupt` cancels the in-flight asyncio task immediately. `user_input` or `user_interrupt(text=…)` events appended during or after the interrupt are read on the next iteration — they are in order on disk, the daemon reads in order, done.
- `user_interrupt(text=…)` behaves like a `user_input` that interrupts first. The event itself enters LLM context as a user turn.
- A bare `user_interrupt(text=None)` cancels the in-flight tick without adding content. `Session._handle_explicit_interrupt` additionally cascades into `BackgroundTaskManager.kill()` for every non-terminal panel entry (bash subprocess groups via `SIGKILL`; sub-agent children via `send_interrupt` → `stop_session`). A chat-with-interrupt (i.e., a `user_input` that arrives mid-run) does not cascade — only the bare ⚡ does.
- `SubAgentTool.execute` wraps `await _wait_for_reply` so cancellation at the parent also `send_interrupt`s the blocking child.

## Interrupt semantics — single event

`user_interrupt` covers both user roles: cancel-only (`text=None`) and cancel-then-send (`text=<msg>`). No separate "cancel" vs "new input" event type. The LLM context builder treats `user_interrupt(text=…)` as a user-role text block; `text=None` events are `for_llm=True` but contribute no content (rendered as an empty turn, collapsed by adjacent-message grouping).

## Tool execution

Each `agent_tool_call` pairs with exactly one `agent_tool_result` keyed by `tool_use_id`. For blocking tools the result lands in the same iteration. For background tools (`run_in_background=true`, bg bash, background sub-agents), the daemon appends `agent_tool_call` immediately + a `panel_entry_changed` so the UI shows "running", then appends `agent_tool_result` when the `BackgroundTaskManager` notifies completion. Intermediate output streams as `tool_progress` events keyed by the same `tool_use_id`.

When the inline tool result is the bg-spawn placeholder (`"Task started. task_id=…"`), `on_tool_done` additionally emits `agent_bg_tool_dispatched` (for_llm=False) so the frontend can keep the cell yellow until the deferred `agent_tool_result` lands. Similarly, `on_thinking_start` emits `agent_thinking_start` (for_llm=False) paired by `block_id` with the canonical `agent_thinking` close event. Both markers are pure presentation hints — `build_llm_context` filters them out.

The live UI renders the call card in "running" state and upgrades to "done" on the result — see [docs/ui/web/design.md](../../ui/web/design.md).

## Stop / Start ↔ task cards

`stop_session` emits `control_stop` and calls `pause_all_cards(tasks_dir)` so every `pending`/`working` card flips to `paused`. `start_session` emits `control_start` and calls `resume_all_paused_cards(tasks_dir)`. Two race guards keep Stop + racing tick-cancel deterministic:

- `_do_tick` re-reads the card from disk on entry; if `paused`/`finished`, it returns immediately without `mark_working`.
- `_dispatch_one`'s TaskItem `CancelledError` handler re-reads the card before `mark_pending`; if `paused`/`finished`, the mark is skipped.

Either guard alone is clobbered by the other; both together converge on `paused`.

## Task-card scheduling

Task cards are tuples of `<name>.json` + `<name>.sh`. The runtime polls the script on the card's `check_interval` cadence via `_poll_card_script`. The LAST non-empty stdout line decides:

- `[start]` → enqueue `TaskItem(card, seed="")`; `_do_tick` runs the agent.
- `[start] <msg>` → same, with `<msg>` as the wakeup seed.
- `[skip]` → stamp `last_checked_at`; do nothing else.
- `[done]` → `card.mark_terminal()` in place; no wakeup, no hook, no `user_input` event.
- Non-zero exit / timeout / unparseable → fail-closed `[skip]` + `task_script_error` event.

All time logic lives inside the agent's bash; the runtime is stateless beyond cadence. Each script runs with a 10 s timeout; timeout reaps the process group.

## Lifecycle hooks

Agents drop `sessions/<id>/core/hook/<event>/main.sh` to react to `session_start`, `agent_loop_start`, or `agent_loop_end`. Each hook receives a `{event, session_id, data}` JSON envelope on stdin, hard-caps at 30 s, and writes a `hook_run` event. Observe-only: exit codes are logged but do not block or mutate agent work.

## Sub-agent identity

`init_session` accepts `parent_session_id`, `mode` (`"explorer"` | `"executor"`), `initial_message_id`, and `display_name`. These are persisted on `manifest.json` and — when present — route the child through guardian-wrapped tools (see [docs/butterfly/core/guardian.md](../core/guardian.md)).

## Team sessions (kind: team)

Sessions whose manifest declares `kind: team` run a different daemon shape: instead of an `Agent` loop, the watcher (`butterfly/runtime/watcher.py:_start_session`) instantiates a `TeamSession` (`butterfly/session_engine/team_session.py`) whose `run_daemon_loop` only routes user input. There is no LLM at this level — the team session is a dispatcher.

Setup: `init_team_session` lays out the team's `sessions/<team_id>/` + `_sessions/<team_id>/` and **spawns one regular sub-agent session per declared member**, recording each member's child session id in `core/members.json` and writing a `TYPE_SUB_AGENT` panel entry per member so the existing UI renders them as sub-session cards.

Routing rules:

  * `user_input` rows on the team's own `context.jsonl` with `caller="human"` are forwarded by `TeamRouter` to the matching member's `BridgeSession` via `send_message(mode="interrupt")`. Recipient is the first `@<name>` in the body that resolves to a real member, falling back to the configured `leader`.
  * Mention parsing is delegated to `teamchat.parse_mentions` so the router agrees with `teamchat_send` on case-insensitivity and the `(?<![A-Za-z0-9_])` lookbehind that keeps email addresses out of the recipient pool.
  * Synthetic `user_input` rows produced by the `teamchat_send` tool (where `caller=<member_name>`) are visible to the UI but skipped by the router so members don't recursively echo each other.
  * `_initial_input_offset()` returns 0 on a fresh team session (signalled by an empty `events.jsonl`) so an `initial_message` written by `init_team_session` is picked up; otherwise it returns end-of-file to avoid replaying already-routed rows on a watcher restart.

See [docs/butterfly/session_engine/agent_team.md](agent_team.md) for the full team layout, the `default` / `silent` member modes, and the persistent `core/teamchat.jsonl` log.
