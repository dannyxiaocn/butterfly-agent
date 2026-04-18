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

### Input dispatcher — interrupt / wait modes (v2.0.12)

`Session` no longer drives the agent loop straight from `chat()`. Every chat call, every drained background-tool notification, and every due task card is enqueued into a single in-memory inbox; a dedicated consumer task drains it serially. The two modes the queue supports correspond exactly to the user-visible verbs the web UI used to fake on the frontend:

| Mode | Producer defaults | Effect on inbox / running run |
| --- | --- | --- |
| `interrupt` | chat from UI/CLI; background-tool notifications (`source=panel`) | Cancel the in-flight run if any; append to inbox. If the cancelled run had not yet committed an assistant turn, the consumer folds the cancelled item's content into this item via `merge_before` so the LLM only sees one user turn (no consecutive `user` messages). |
| `wait` | task wakeups (`source=task`); explicit `mode=wait` chats | Append to inbox without cancelling. If the trailing inbox entry is itself a wait-mode `ChatItem`, merge into it via `merge_after` so a burst of "...also" sends collapses into one user turn. |

A bare `send_interrupt()` (the explicit ⚡ Interrupt button) cancels the in-flight run **and** drops the entire inbox — it is not a chat-with-mode; it just stops.

#### Why the cancelled-merge rule is non-negotiable

`Agent.run()` builds `messages = [..._history, user_message]` and writes nothing to `_history` until after each iteration commits an assistant turn. Cancellation mid-`provider.complete()` therefore leaves history clean — the user turn was never durably appended. If the dispatcher then sent the new content as a fresh user message, the next agent run would still build `[..._history, new_user]`, but the **prior on-disk state** (any earlier turns) would still end with whatever was last committed; the *intended* user turn for the cancelled chat is lost. By folding the cancelled content into the new chat (`merged = old + new`), the LLM receives the full intent without any consecutive-user-message violation.

For the symmetric case — cancellation **after** at least one iteration commits — `_do_chat` writes a `turn` event with `interrupted: True` carrying just the committed prefix. The new chat then runs as a fresh user turn (history already ends with the committed assistant message, so consecutive-user-message is impossible).

#### Per-iteration history commits (`Agent.run`)

The dispatcher's "uncommitted vs committed" decision needs a sharp signal. `Agent.run()` therefore writes `self._history = list(messages)` after each iteration's assistant append (and again after the tool-result append), so cancellation at any point reflects exactly what the LLM has produced. The trailing `self._history = list(messages)` after the loop is now redundant but harmless.

#### Task-card scheduling

Task cards enter the inbox as `TaskItem(card=...)`. `_scheduled_task_names` is a guard the daemon uses to skip a card it has already enqueued so the same wakeup isn't queued every 0.5 s while it sits behind a chat. `TaskItem` never merges with chat items — task wakeups have their own prompt template and `mark_working` / `mark_finished` bookkeeping. If a wait-mode chat arrives while a task is queued behind a chat, both run in their own activations, in order.

#### Frontend implications

The 5 s pending bar (PR #24) is removed. The dispatcher owns merging, so each Enter sends one `POST /messages` with a `mode` field — default `interrupt`. The Alt/⌥+Enter shortcut (and a small `wait` checkbox) sends with `mode=wait`. There is no per-tab buffer to go stale across tabs, which fixes the silent-cross-session footgun that PR #24 review flagged.

#### Daemon poll cadence — split input / housekeeping (v2.0.14)

`run_daemon_loop` polls on two cadences, tuned by class constants on `Session`:

| Constant | Value | Scope |
| --- | --- | --- |
| `_INPUT_POLL_INTERVAL` | `0.05` (50 ms) | Drains `context.jsonl` for new `user_input`, the explicit-interrupt control event on `events.jsonl`, and `BackgroundTaskManager` completion notifications every tick. |
| `_TASK_POLL_INTERVAL` | `0.5` (500 ms) | Housekeeping: stopped-session auto-expiry after 5 h, plus scanning `core/tasks/` for due cards to enqueue. |

Why split: with a single 500 ms cadence, a fast provider could finish the **first** chat before the daemon ever saw the **second** message, so the two runs produced two separate turns instead of the expected cancel-and-merge behaviour described above. A 50 ms input poll closes that race while leaving task scheduling on the coarser cadence — task cards have `interval`s measured in seconds to hours, so a sub-second check serves no purpose. The loop body calls `self._drain_background_events()` and `ipc.poll_interrupt()` / `ipc.poll_inputs()` every tick; the housekeeping block runs only when `loop.time() >= next_housekeeping_at`, which is advanced by `_TASK_POLL_INTERVAL` each firing.

`_scheduled_task_names` still guards a card against being re-enqueued while it sits in the inbox or is currently running, so the faster input tick cannot multi-queue the same wakeup.

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
