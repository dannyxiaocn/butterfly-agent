# Session Engine — Design

The session engine is the **bridge between static agent definitions and live runtime sessions**.

## Responsibilities

- Parse agent `config.yaml` manifests
- Build `Agent` objects from fully self-contained agent directories
- Manage meta sessions (agent → meta → child session lifecycle)
- Create session directory structures on disk
- Wrap `Agent` with persistent, file-backed `Session` behavior
- Track agent versions and notify stale sessions

## Key Concepts

### Agent → Meta → Session Flow

```
agenthub/<name>/          ← static template, version-controlled
  → populate_meta_from_agent()  ← one-time seed at meta session creation
    → sessions/<agent>_meta/   ← authoritative living config (evolves independently)
      → init_session()           ← each new child session
        → sessions/<id>/         ← child session (seeded from meta, then independent)
```

### Meta Sessions

Each agent has a meta session (`<agent>_meta`) that:
- Holds the canonical, evolving config for all future child sessions of that agent
- Acts as shared mutable state store (memory, playground)
- Runs as a real persistent agent with "dream cycle" task schedule
- Maintains `agent_version` in `core/config.yaml`
- Syncs improvements back to `agenthub/` via PRs on the `mecam/agent-update` branch

### Agent Templates

`agenthub/<name>/` is a **static seed**, not a live config:
- Used once to bootstrap the meta session
- Each agent is fully self-contained — all prompts, tools, skills are physically present
- `init_from` in `config.yaml` documents provenance but has no runtime effect
- New agents are created with `butterfly agent new --init-from <source>` (one-time copy) or `--blank`

### Version Staleness Notices

When a child session starts its daemon loop, it compares its `agent_version` against the meta session's current version. If meta has advanced, a `system_notice` event is emitted — rendered in both web UI and CLI — suggesting the user start a new session to pick up the latest configuration.

### Input dispatcher — two-queue model (v2.0.24)

`Session` doesn't drive the agent loop from `chat()` directly — every chat call, every drained background-tool notification, and every due task card is enqueued into the dispatcher; a dedicated consumer task drains it serially. v2.0.24 replaced the v2.0.12 single-inbox model with **two independent queues** so the spec's intent ("interrupt always aggregates and pre-empts; wait always queues") falls out of the data structure rather than from per-pop merge predicates.

```
self._interrupt_queue: list[ChatItem]              # mode=interrupt only
self._wait_queue:      list[ChatItem | TaskItem]   # mode=wait + every TaskItem
```

| Producer | Routing |
| --- | --- |
| `ChatItem(mode=interrupt)` — chat from UI/CLI; background-tool notifications (`source=panel`); background sub-agent completions | Append to `_interrupt_queue`. If a run is in flight, cancel `_run_task` immediately. |
| `ChatItem(mode=wait)` — explicit ⌥+Enter from UI; SDK `mode="wait"` callers | Append to `_wait_queue`. If the queue's tail is itself a chat-wait, `merge_after` so consecutive "...also" sends collapse into one user turn. |
| `TaskItem` — every due task card (heartbeat, user-defined, etc.) | Append to `_wait_queue`. Never merges with anything (cards own per-name `mark_working` / `mark_finished` / `SESSION_FINISHED` rollback bookkeeping that wouldn't survive textual merge). |

**Consumer rule** — interrupt always wins:

1. If `_interrupt_queue` is non-empty, drain the **whole** queue, fold every item via `merge_after` into one `ChatItem`, and dispatch it. A burst of cancel-and-aggregate arrivals therefore runs as a single LLM turn — the consumer never lets two interrupt items dispatch separately.
2. Otherwise, pop one item from `_wait_queue`. If it's a chat-wait, drain the chat-wait tail at the head of the queue into it the same way `_enqueue` does (catches arrivals that race the per-arrival merge). `TaskItem` falls straight through — pop one and dispatch.
3. Both queues empty → consumer exits; `_enqueue` restarts it on the next arrival.

**Bare `send_interrupt()`** — the explicit ⚡ button — cancels `_run_task`, clears **both** queues, rejects every queued future, and cascades into `BackgroundTaskManager.kill()` for every non-terminal panel entry (bash bg subprocesses + background sub-agents). Distinct from a chat-with-mode=interrupt: this one runs nothing in its place.

#### Why the cancel-and-fold rule is non-negotiable

`Agent.run()` builds `messages = [..._history, user_message]` and writes nothing to `_history` until each iteration commits an assistant turn. Cancellation mid-`provider.complete()` therefore leaves history clean — the user turn was never durably appended. If the dispatcher then sent the new content as a fresh user message, the next agent run would still build `[..._history, new_user]`, but the **prior on-disk state** would still end with whatever was last committed; the *intended* user turn for the cancelled chat is lost. So when an in-flight chat is cancelled by an interrupt arrival, `_dispatch_one` checks `committed = len(history) > baseline`:

- **Uncommitted** → fold the cancelled prefix into the head of `_interrupt_queue` via `merge_before`. The next consumer iteration drains the queue and merges; the cancelled prefix appears at the start of the aggregated turn.
- **Committed** → `_do_chat` writes a `turn` event with `interrupted: True` carrying just the committed prefix. The aggregated interrupt then runs as a fresh user turn (history already ends with a committed assistant message, so consecutive-user-message is impossible).

Tick cancellation is simpler: the card is `mark_pending`-ed and re-fires on the next due check; cancelled wakeup content is discarded (task prompts don't textually merge with chat content). Consequence on **meta sessions** (whose only activity is a heartbeat `task_wakeup` card): a bare ⚡ cancels *this* tick but the card's `last_finished_at` is not stamped, so `is_due()` returns True on the next 500 ms housekeeping poll and the tick restarts almost immediately. This is intentional — a heartbeat is supposed to beat. To actually quiet a meta session, use the **Stop** button, which additionally calls `pause_all_cards` (see below) so no card re-fires until ▶ Start.

Two race guards (v2.0.26) cooperate to make Stop + racing tick-cancel deterministic:

- `_do_tick` reads the card's on-disk status before calling `mark_working`; if it's already `paused` or `finished` (racing Stop or a Finish marker landed while this tick was in the queue), the tick returns immediately without running the wakeup prompt.
- `_dispatch_one`'s TaskItem CancelledError handler re-reads the card from disk before calling `mark_pending`; if the on-disk status is `paused` or `finished`, the mark_pending is skipped.

Either guard alone would be insufficient — `_do_tick` runs `mark_working` right at entry, which would overwrite a paused disk marker; the cancel handler alone can't fix that because the tick may have already written `working`. Both checks together ensure a Stop during a tick converges cleanly on `paused`.

#### Uniform cancel path for ChatItem and TaskItem

`_dispatch_one` wraps **both** branches in `self._run_task = asyncio.create_task(...)`, then awaits it. Pre-v2.0.24 the TaskItem branch awaited `_do_tick` inline, so `_run_task` stayed `None` while a tick was running — `_enqueue`'s interrupt-cancel hook (and `_handle_explicit_interrupt`) had nothing to call `cancel()` on, and the in-flight tick ran to completion with the interrupting chat queued behind it. Meta sessions felt this hardest: their only activity is the heartbeat `task_wakeup` tick, so the ⚡ button was effectively a no-op on them. Routing both paths through the same handle lets cancellation propagate uniformly; tick CancelledError routes through the TaskItem branch (mark card pending, reject futures), chat cancel walks the uncommitted-merge / committed-partial-save fork above.

#### What v2.0.24 simplified away

The v2.0.12 single-inbox model encoded merging as "merge into the trailing same-mode item" and added v2.0.21 "consumer-side same-mode tail merge" on top. Both checks compared `item.mode == inbox[-1 / 0].mode`, which only worked when arrivals were perfectly ordered — interleaved modes broke the invariant. The two-queue model retires both predicates: queue membership encodes mode, drain rules encode merge intent. Interrupt always-aggregates is now **structural** ("drain the whole queue at dispatch"), wait-tail merge is contained to the chat-wait case, and `TaskItem` automatically lives in the right queue without a `mode` field.

#### v2.0.24 — bare-interrupt cascade to background workloads

`Session._handle_explicit_interrupt` now also calls `_cascade_interrupt_background()` after cancelling `self._run_task` and clearing the inbox. The cascade walks `self.panel_dir`, finds every non-terminal panel entry, and calls `BackgroundTaskManager.kill(tid)` on each. The runner-specific `kill` does the right thing per type:

- `BashRunner.kill` — `os.killpg(SIGKILL)` on the subprocess group.
- `SubAgentRunner.kill` — `BridgeSession.send_interrupt()` to the child session **then** `stop_session()` (was just `stop_session` before — that path only set `status=stopped`, but the child's stopped check fires only when a fresh input arrives, so the child's in-flight chat kept running until a natural break).

Per spec, this only fires on the **bare** ⚡ button. A chat with `mode=interrupt` (i.e., a new user input arrives during a run) leaves background workloads alone — the cancel propagates only through the await chain rooted at `_run_task`, which reaches *blocking* sub-agents (awaited inside `_execute_tools`) but not background runners (which live on the `BackgroundTaskManager`'s own task list).

#### v2.0.24 — blocking sub-agent cascade in `SubAgentTool.execute`

Even with the new bare-interrupt cascade, a chat-with-mode=interrupt that lands while the parent is mid-`sub_agent(run_in_background=false)` would previously leave the child session daemon churning on a discarded task: cancellation would propagate up through `_execute_tools` and `SubAgentTool.execute()`, but the child daemon, started by `init_session` and adopted by `SessionWatcher`, runs independently of that await chain. `SubAgentTool.execute` now wraps the `await _wait_for_reply` in a try/except that calls `BridgeSession.send_interrupt()` on the child before re-raising — same primitive the bare-interrupt cascade uses on background sub-agents, applied here at the blocking-sub-agent boundary. Best-effort; failures are swallowed so cancellation propagation stays clean.

#### Per-iteration history commits (`Agent.run`)

The dispatcher's "uncommitted vs committed" decision needs a sharp signal. `Agent.run()` writes `self._history = list(messages)` after each iteration's assistant append (and again after the tool-result append), so cancellation at any point reflects exactly what the LLM has produced.

#### Task-card scheduling

Task cards enter `_wait_queue` as `TaskItem(card=...)`. `_scheduled_task_names` guards a card from being re-enqueued while it already sits in the queue or is currently running — without it, the daemon's housekeeping tick (every 0.5 s) would re-due the same card every cycle. `TaskItem` never merges with chat items: each wakeup owns its own prompt template (`agent.task_prompt`) and per-card bookkeeping (`mark_working` / `mark_finished` / `SESSION_FINISHED` rollback) that wouldn't survive textual merge. Wait-mode chat arrivals queue alongside in arrival order; each runs in its own activation.

#### Stop / Start ↔ task-card pause / resume (v2.0.24)

The web sidebar's Stop button calls `service.stop_session`, which now also calls `pause_all_cards(tasks_dir)` so every `pending` / `working` card flips to `paused`. Without this, the daemon's `is_stopped()` short-circuit just skipped the housekeeping branch — the cards stayed `pending`, and the moment the user hit ▶ Start every overdue wakeup would fire at once. Symmetrically, `start_session` calls `resume_all_paused_cards(tasks_dir)` to flip every `paused` card back to `pending`. Cards manually paused via CLI/UI also un-pause on Start; if separate persistence is needed, the user re-pauses after resume. Both helpers are best-effort — Stop / Start succeed even if the disk write fails, and the per-context "paused — use ▶ Start to resume" / "resumed" status rows that the pre-2.0.24 service emitted are dropped (the sidebar renders the stopped state from `/api/sessions`; the context-stream notice was redundant chrome).

#### Frontend implications

The 5 s pending bar (PR #24) is removed. The dispatcher owns merging, so each Enter sends one `POST /messages` with a `mode` field — default `interrupt`. The Alt/⌥+Enter shortcut (and a small `wait` checkbox) sends with `mode=wait`. There is no per-tab buffer to go stale across tabs, which fixes the silent-cross-session footgun that PR #24 review flagged.

#### Daemon poll cadence — split input / housekeeping (v2.0.14)

`run_daemon_loop` polls on two cadences, tuned by class constants on `Session`:

| Constant | Value | Scope |
| --- | --- | --- |
| `_INPUT_POLL_INTERVAL` | `0.05` (50 ms) | Drains `context.jsonl` for new `user_input`, the explicit-interrupt control event on `events.jsonl`, and `BackgroundTaskManager` completion notifications every tick. |
| `_TASK_POLL_INTERVAL` | `0.5` (500 ms) | Housekeeping: stopped-session auto-expiry after 5 h, plus scanning `core/tasks/` for due cards to enqueue. |

Why split: with a single 500 ms cadence, a fast provider could finish the **first** chat before the daemon ever saw the **second** message, so the two runs produced two separate turns instead of the expected cancel-and-merge behaviour described above. A 50 ms input poll closes that race while leaving task scheduling on the coarser cadence — task cards have `interval`s measured in seconds to hours, so a sub-second check serves no purpose. The loop body calls `self._drain_background_events()` and `ipc.poll_interrupt()` / `ipc.poll_inputs()` every tick; the housekeeping block runs only when `loop.time() >= next_housekeeping_at`, which is advanced by `_TASK_POLL_INTERVAL` each firing.

`_scheduled_task_names` still guards a card against being re-enqueued while it sits in `_wait_queue` or is currently running, so the faster input tick cannot multi-queue the same wakeup.

#### `merged_user_input_ids` on `turn` events

When the dispatcher folds multiple `user_input` events into one user message (the `merge_before` / `merge_after` paths above), the resulting `turn` event on `context.jsonl` records every contributing id:

```jsonc
{ "type": "turn",
  "user_input_id": "<last id — back-compat>",
  "merged_user_input_ids": ["<id_1>", "<id_2>", ...],
  "messages": [...], "usage": {...}, "ts": "..." }
```

`user_input_id` is preserved (it is the id of the final merged input) so pre-merge consumers still work. `merged_user_input_ids` is populated only when more than one input contributed — a single-input turn omits it. Consumers that walk history must honour the merged list, otherwise earlier inputs of a merged turn look "pending" or invisible:

- `butterfly.service.history_service.turn_input_ids / turn_user_content / turn_display_ts` — canonical helpers. `get_pending_inputs` excludes every id in the merged list; `get_log_turns` / `get_token_report` concatenate content and use the earliest timestamp.
- `butterfly.runtime.bridge.BridgeSession.async_wait_for_reply` — matches a caller's `msg_id` against `merged_user_input_ids` as well as `user_input_id`, so SDK callers (e.g., WeChat) whose original id was not the *final* merged id do not time out.
- `ui/cli/chat.py._read_matching_turn` — same matching rule for the interactive CLI.
- `ui/cli/main.py` `butterfly log` — reuses the `history_service` helpers (no local reimplementation); merged turns render with concatenated user content and the earliest input timestamp.

### `init_session()` invariant — manifest.json is the watcher's discovery signal

`_sessions/<id>/manifest.json` is what `SessionWatcher._scan()` checks to decide whether to spawn a `Session` task for a given session_id. Therefore:

- **manifest.json MUST be written last** in `init_session()`, only after `sessions/<id>/core/config.yaml` (and any other required seed files) is fully populated from the agenthub/meta.
- If manifest.json is published early, the server-side watcher can race `init_session()` and spawn `Session(session_id)` whose `Session.__init__` calls `ensure_config(session_dir)` → that writes `DEFAULT_CONFIG` (with `model=None`, `provider=None`) into the session core before `init_session` gets a chance to copy the real config. Once the stub is on disk, the `if not session_config_path.exists()` guard inside `init_session()` would silently skip the copy, leaving the session permanently stuck on `model: null`.
- As a belt-and-braces safeguard, `init_session()` also treats a config with `model` unset/null as "still needs seeding" rather than a finished session config. This way, even if a different code path writes a stub config first, the agent's model/provider still make it onto disk.

This invariant was added in v2.0.8 after a first-run repro: `butterfly-server` daemon + `butterfly new` would consistently produce `sessions/<id>/core/config.yaml` with `model: null`.

## Memory layers — on-demand recall (v2.0.5, β)

**Change from v2.0.x**: previously, every file under `core/memory/*.md` was loaded into `Agent.memory_layers` and injected into the system prompt (with a 60-line truncation). Starting v2.0.5, sub-memory is **not** injected into the prompt. Only `core/memory.md` (main) is.

### Structure

```
sessions/<id>/core/
├── memory.md              ← main memory, always in system prompt
└── memory/
    ├── dev_sop.md         ← sub-memory layer
    ├── repo_map.md        ← sub-memory layer
    └── ...
```

### Main-memory index convention

`memory.md` contains one line per sub-memory file under a `## Memory files` section:

```markdown
## Memory files
- dev_sop: SOP the agent has to follow when developing tools/skills
- repo_map: Cached map of key modules and their responsibilities
```

The agent discovers available sub-memories by reading main memory (which is always in prompt). To access a sub-memory's full contents, the agent calls `memory_recall(name="dev_sop")`.

### Write path

Sub-memory is edited exclusively via `memory_update(name, old_string, new_string, description?)`:
- Creates `core/memory/<name>.md` on first write, applying `new_string` as initial content.
- On subsequent writes, behaves like `edit`: exact replacement with uniqueness enforcement.
- Always upserts the index line `<name>: <description>` in main memory. On first-time creation, `description` is required.

Main `memory.md` itself is edited via `edit` / `write` like any other file — no dedicated tool.

### Why this change

1. **Prompt budget** — long-running sessions accumulate sub-memory layers that would otherwise bloat the system prefix. On-demand recall keeps the static prefix small and cache-friendly.
2. **Explicit retrieval** — agent decides what it needs; no silent truncation of layers it was relying on.
3. **Index discipline** — requiring a one-line description per layer forces the agent to keep a readable map in main memory, which is what a human skim-reader also wants.

### Session impl impact (for implementers)

- `Session._load_session_capabilities` stops populating `self._agent.memory_layers` from `core/memory/*.md`. The attribute is removed from the `Agent` class.
- System-prompt assembly (in `Agent`) drops the `memory_layers` rendering block.
- `memory_recall` and `memory_update` tool executors share a `memory_dir` + `main_memory_path` context injected by `ToolLoader`.

---

## Sub-agent identity (v2.0.13)

`init_session()` accepts three additional keyword args used by the
`sub_agent` tool when it spawns a child:

- `parent_session_id: str | None` — recorded in `manifest.json` so the
  sidebar can group child sessions under their parent.
- `mode: "explorer" | "executor" | None` — when set, the matching
  `toolhub/sub_agent/<mode>.md` is copied to the child's
  `core/mode.md`. The mode name is also persisted to the manifest.
- `initial_message_id: str | None` — lets the caller pre-pick the UUID for
  the seeded `user_input` event so it can later call
  `BridgeSession.async_wait_for_reply(msg_id)` and correlate the response.
- `display_name: str | None` (v2.0.19) — user-facing label stored on
  `manifest.json`. Sidebar/panel prefer this over the raw `session_id`.
  Normalized via `_normalize_display_name` (trim + truncate to 40 chars);
  empty/blank becomes `None` (no manifest entry written). Also settable
  from the web `POST /api/sessions` body, so the new-session form can
  ask the user for a name while the server keeps auto-generating the
  canonical `session_id`.

### Manifest schema (additive)

```jsonc
{
  "session_id": "2026-04-16_21-30-11-abcd",
  "agent": "agent",
  "created_at": "...",
  // present only on sub-agent children:
  "parent_session_id": "<parent id>",
  "mode": "explorer",
  // present only when a display_name was provided (v2.0.19):
  "display_name": "audit auth flow"
}
```

The manifest-last invariant from v2.0.8 is preserved — these new fields
are written in the same final payload, not in a separate file.

### Mode prompt slot

`Session._load_session_capabilities` reads `core/mode.md` after `system.md`
and concatenates: `system_prompt = system_md + "\n\n---\n\n" + mode_md`
(or just `system_md` when `mode.md` is absent). The mode prompt thus lives
inside the cacheable static prefix Anthropic ephemeral-caches; it is not
re-rendered per turn.

### Guardian wiring

When `manifest.mode == "explorer"`, `Session.__init__` constructs
`Guardian(playground_dir)` and threads it through `ToolLoader` into the
Write / Edit / Bash executors. See `docs/butterfly/core/guardian.md` for
the boundary contract and `docs/butterfly/tool_engine/design.md` §12 for
the broader sub-agent flow.

---

## Agent output duration — per-turn positional pairing (v2.0.20)

`Session` buffers each LLM call's **text-output** duration in a list
(`self._current_turn_agent_durations`) that lives for the span of one chat
or tick run. `_make_llm_call_end_callback` appends an entry whenever the
call produced text (i.e. `_text_output_started_at` was stamped by the
chunk callback during that call). The turn writer drops the list onto
`turn["agent_output_durations"]` — parallel to `thinking_blocks`.

### Invariants

1. The list is **reset at the start** of each `_do_chat` / `_do_tick`,
   before any callbacks are installed, so a prior run's leftovers can
   never bleed into the next turn.
2. Entries are **ordered chronologically** — the same order LLM calls
   fire during `Agent.run()`, and therefore the same order text content
   blocks appear in the turn's assistant messages.
3. `FileIPC._context_event_to_display` pairs the list with text blocks
   **positionally WITHIN the same turn**: a local cursor, initialized
   per turn, advances once per non-empty text block. Surplus text blocks
   (rare provider quirk: one LLM call emits two text blocks) render
   without a duration pill; surplus durations are ignored.

Cross-turn positional pairing (earlier prototype, now removed) was
fragile: any turn without the instrumentation — e.g. turns written
before this release — shifted the cursor and attributed call N+1's
duration to call N on reload, so the same cell could show 0.3 s live
and 0.7 s after a page reload.

## Interrupted thinking — placeholder-on-start (v2.0.20)

`_make_thinking_callbacks` seeds the collected list on **every**
`on_thinking_start` with an entry carrying `interrupted=True`:

```python
collected.append({
    "block_id": block_id,
    "text": "",
    "ts": datetime.now().isoformat(),
    "interrupted": True,
})
```

`on_thinking_end` locates the entry by `block_id` and upgrades it in
place: fills `text` + `duration_ms`, clears the `interrupted` flag.
Successful turns therefore persist an `interrupted`-free
`thinking_blocks` list.

A turn cancelled between the two callbacks leaves one or more
placeholders un-upgraded. `_save_partial_chat_turn` persists them as-is,
and `ipc.py._context_event_to_display` forwards the `interrupted` flag
onto the replayed `thinking` display event so history replay renders
"Thinking interrupted" for those blocks instead of a bland "Thought 0.0s".

Known limitation: `pending` is a **LIFO stack**. Providers that emit
non-nested thinking block pairs (`start1 → start2 → end1 → end2`) would
upgrade the wrong placeholder. None of the currently supported providers
(Anthropic / Codex / Kimi) emit this pattern, so the constraint is
adequate in practice; revisit if a future provider requires ordered
pairing by `block_id`.

## Tool lifecycle — `is_error` on `tool_done` (v2.0.23)

`_make_tool_done_callback` grew a 5th positional arg: `on_tool_done(name, input, result, tool_use_id, is_error)`. The flag is computed at the agent layer (`core/agent.py::_execute_tools`) by combining:

1. The existing exception branches (tool-not-found, background-spawn-fail, `tool.execute()` raised) that already stamped `is_error=True`.
2. `butterfly.tool_engine.classify_tool_result(name, content)` on the success-return path (see `docs/butterfly/tool_engine/design.md` §6.1).

When true, the value is written into the `tool_done` event on `events.jsonl` as `is_error: true`. The paired `tool_result` block in `context.jsonl` already carried the same bit since v2.0.5, so history replay doesn't regress — it simply means the frontend can colour the live cell red from the first SSE frame instead of waiting for a reload.

External `Session(on_tool_done=...)` hooks are still invoked with the pre-v2.0.19 3-arg shape (`name, input, result`). The 5-arg signature is internal to the Agent → Session contract.

## Interrupt sweep — tool + thinking cell terminal state (v2.0.23)

Bare interrupt (`bridge.send_interrupt()` → control event on `events.jsonl` → `Session._handle_explicit_interrupt` cancels `self._run_task`) always works end-to-end on the backend: `cancelled_run=true` + `model_status=idle` are emitted. What the web UI used to miss: a tool call cancelled mid-flight never produces a `tool_done`, so the yellow `▶ bash running…` cell span for the lifetime of the next run, and the user saw "nothing happened".

The frontend fix lives entirely in `chat.ts::markRunningToolsInterrupted` — called from the `model_status: idle` branch next to the pre-existing `markRunningThinkingInterrupted`. It sweeps every `.msg-tool:not(.done)` into a terminal `done interrupted` state (dim yellow chrome + `✗ interrupted Xs`) and clears the `runningTools` / `backgroundCells` maps + HUD. Safe to call on every idle transition: a clean run has already transitioned every tool to `.done` via its `tool_done`, so both maps are empty and the DOM scan is a no-op.

The backend `"interrupt" / "interrupted"` control events remain intentionally filtered out of `_runtime_event_to_display` — the UI needs no direct event handler, only the `model_status=idle` sweep.

## External hooks — agent-authored lifecycle scripts (v2.0.27)

Agents drop a single `main.sh` under `sessions/<id>/core/hook/<event>/` to react to session lifecycle events. Three events are wired: `session_start` (daemon start), `agent_loop_start` (before every `Agent.run`), `agent_loop_end` (after — `data.reason` ∈ `finished`/`cancelled`/`error`). The runtime fires each via `external_hooks.run_hooks()` which pipes a `{event, session_id, data}` JSON envelope over stdin, runs `bash <event>/main.sh`, hard-caps at 30 s, and writes a `hook_run` event to `events.jsonl` with exit code, duration, stdout/stderr tails.

Only `main.sh` is inspected — the agent owns ordering and conditional execution inside the script (calling any number of siblings it wrote). Keeping it one file per event kept timeout / output capture semantics simple: one timer, one stream cap (4 KB per stream), one exit code.

Observe-only baseline: exit codes are logged but do NOT block or mutate the pending agent work. A future release may add a blocking opt-in for `session_start` and `agent_loop_start` — TODO `blocking-hooks` tracks it. `agent_loop_end` should remain observe-only since the loop has already ended.

## Bash-driven task cards (v2.0.27)

Task cards drop all time fields (`start_at` / `end_at` / `interval` as trigger). A card is a tuple of JSON metadata + `<name>.trigger.sh` + optional `<name>.end.sh`. The runtime polls the scripts on the card's `check_interval` cadence via `session._poll_trigger_script` / `_poll_end_script`:

- `trigger.sh` last line `[start]` → enqueue `TaskItem(card, seed=<rest-of-line>)` into the wait queue. The seed is appended to the generated wakeup prompt as `Trigger reason: <seed>`.
- `trigger.sh` last line `[skip]` → stamp `last_checked_at`, move on.
- Non-zero exit / timeout / unparseable → fail-closed (`[skip]` + `task_check_error` event).

All time logic lives inside the agent's bash (e.g. `[[ $(date +%H) -ge 9 ]] && echo [start] || echo [skip]`) — the runtime is stateless beyond cadence. Scripts run serially during the housekeeping tick; each has a 10 s timeout (`task_runner._CHECK_TIMEOUT_SEC`) so a misconfigured check never stalls the daemon.

Every run emits a `task_check` event carrying `card_name`, `kind` (`trigger`/`end`), `tag`, truncated `stdout`/`stderr`, `exit_code`, `duration_ms` — the UI surfaces these in the panel so agents and operators can see why a card did (or did not) fire without re-running the script.
