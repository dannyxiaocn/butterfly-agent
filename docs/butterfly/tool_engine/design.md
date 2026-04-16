# Tool Engine — Design

The tool engine turns tool definitions into executable `Tool` objects. Tools live in `toolhub/` (centralized, repo-wide) and are enabled per-entity via `tools.md` (one tool name per line; legacy `tool.md` is still accepted as a fallback but `tools.md` is canonical). The `ToolLoader` dynamically imports executors at session init and injects session context (workdir, tasks_dir, memory_dir, etc.) into their constructors so agents never pass environmental parameters.

This file is the authoritative spec for v2.0.5. It covers the catalog, the backgroundable protocol, the panel, and the shell / bash split.

---

## 1. Design principles

1. **One tool, one job.** No dispatcher tools. Verb-named tools beat `action:` strings.
2. **Minimal parameters.** Every deterministic-per-session value is injected at ToolLoader time, not by the agent per call.
3. **Schema + description define agent UX.** Both are authoritative; sub-agents building tools must treat them as contracts.
4. **Structured results.** Tools return strings, but those strings are conventionalized (see §6). Large outputs spill to disk.
5. **Append-only history.** Any notification or event the agent needs to see goes to `context.jsonl` exactly once — never re-injected at prompt-build time (see §8 on TTL avoidance).

---

## 2. Tool catalog (v2.0.5)

The catalog is split into **toolhub tools** (declared in `toolhub/<name>/`) and **session-authored tools** (`.json`+`.sh` pairs agents create at runtime).

| Name | Purpose | Backgroundable | Agent sees |
|---|---|---|---|
| `bash` | One-shot shell command, fresh process every call | **Yes** | `command, timeout?, stdin?, run_in_background?, polling_interval?` |
| `sub_agent` | Spawn a child session (same entity), return its FINAL reply | **Yes** | `task, mode (explorer\|executor), timeout_seconds?, run_in_background?, polling_interval?` |
| `session_shell` | Persistent long-lived shell, `cd`/env survive across calls | No | `command, timeout?, reset?` |
| `read` | Read file contents (paginated) | No | `path, offset?, limit?` |
| `write` | Write/overwrite a file | No | `path, content` |
| `edit` | Exact string replacement on a file | No | `path, old_string, new_string, replace_all?` |
| `glob` | Find files by pattern | No | `pattern, path?` |
| `grep` | Search file contents | No | `pattern, path?, glob?, -i?, -n?, output_mode?` |
| `web_search` | Multi-provider search (brave/tavily) | No | `query, count?, freshness?, ...` |
| `web_fetch` | Multi-provider URL fetch + text extraction | No | `url, max_chars?` |
| `skill` | Load a SKILL.md into context | No | `skill, args?` |
| `recall_memory` | Read full sub-memory file | No | `name?` |
| `update_memory` | Edit sub-memory **and** update main-memory index line | No | `name, old_string, new_string, description?` |
| `task_create` | Create a task card | No | `name, description, interval?, start_at?, end_at?` |
| `task_update` | Update a task card (description, interval, progress, comments) | No | `name, ...` |
| `task_finish` | Mark a task card finished | No | `name` |
| `task_pause` | Pause a recurring task | No | `name` |
| `task_resume` | Resume a paused task | No | `name` |
| `task_list` | List all task cards | No | — |
| `tool_output` | Fetch full output of a backgrounded tool call | No | `task_id, delta?` |

**Removed in v2.0.5**:
- `shell` — merged into `bash` (pass `.sh` as command: `bash(command="bash my.sh arg")`)
- `manage_task` — split into six verb tools above
- `reload_capabilities` — removed; a runtime-level filesystem watcher will replace it (tracked in `runtime/todo.md`)

---

## 3. Bash (one-shot) vs session_shell (persistent)

The two cover distinct use cases. Their descriptions point at each other.

### 3.1 `bash` — stateless one-shot

- Every call spawns a fresh subprocess via `asyncio.create_subprocess_shell`.
- `cd`, `export`, aliases **do not persist** across calls.
- Auto-injected `workdir` = session directory (agent uses relative paths).
- Auto-activates session venv (`sessions/<id>/.venv`) if present, via env injection.
- **No PTY.** Removed. Output is clean bytes; stderr merged with stdout via `2>&1` inside command if agent needs separation.
- Optional `stdin: str` parameter is piped to the process for pre-feeding interactive prompts (`bash(command="apt install foo", stdin="y\n")`).
- Backgroundable (see §4).

Structured output (returned as a single string, but formatted):
```
<stdout/stderr combined>
[exit N, duration 2.3s, truncated false]
```
If output > `max_output_chars` (default 10_000), the tail is kept and a `[spilled: _sessions/<id>/tool_results/<uuid>.txt]` line is appended; `tool_output` or `read` can fetch the full file.

### 3.2 `session_shell` — persistent

- **One long-lived `bash --norc --noprofile` per session**, lazily started on first call.
- **Sentinel protocol**: each call writes `{command}\nprintf '\n__BFY_DONE_%d_%d__\n' $RANDOM $?\n` and reads until the marker; the exit code is embedded in the marker.
- Workdir, env vars, aliases, functions persist between calls.
- **Single-command timeout**: sends SIGINT; escalates to SIGKILL + shell restart; caller sees `[timed out after Ns, shell restarted]`.
- **Auto-restart** if the shell dies between calls; next output is prefixed `[shell restarted]`.
- `reset=True`: kills and restarts the shell, clearing all state.
- **Not** backgroundable — long-running work goes in `bash` with `run_in_background=true`. session_shell is for *sequencing*, not for background processes.
- **No parallel calls within one session**; the lock is enforced inside the executor (concurrent calls queue on the shell's stdin).

### 3.3 When to use which (agent-facing doc)

The tool descriptions explicitly point at the other:

- `bash.description`: *"One-shot shell command. Each call is a fresh process; `cd`/`export` do NOT persist. Use for independent commands, file operations, git, tests. For long-running work (> 30s) set `run_in_background=true`. For multi-step workflows that need to share environment (venv activate + run, cd into subdir + run), use `session_shell` instead."*
- `session_shell.description`: *"Persistent shell — all calls share one long-lived bash. `cd`, `export`, aliases, functions persist. Use when setup and subsequent commands must share environment. One command at a time. Not for background processes — use `bash(run_in_background=true)` for those."*

---

## 4. Backgroundable tools (non-blocking execution)

v2.0.5 introduces a uniform opt-in non-blocking protocol. Only tools whose `tool.json` sets `"backgroundable": true` participate; currently that is `bash` alone.

### 4.1 Protocol

When `Tool.backgroundable == True`, the `ToolLoader` automatically:

1. **Adds two fields** to the tool's schema `properties`:
   ```jsonc
   "run_in_background": {
     "type": "boolean",
     "description": "If true, tool starts and returns immediately with a task_id. Use for commands expected to run > 30s, or when you want to keep working while it runs."
   },
   "polling_interval": {
     "type": ["integer", "null"],
     "description": "Seconds between heartbeat deliveries of new output. Omit for stall-watchdog only (recommended)."
   }
   ```
2. **Appends a standard paragraph** to the tool description:
   > *"This tool supports non-blocking execution. Set `run_in_background=true` to start and receive a placeholder result immediately; the real output will be delivered later as a notification in your context, and you can fetch it anytime with `tool_output(task_id)`. A stall watchdog notifies you if the task produces no output for 5 minutes. The task's status is also visible in the session panel."*

Neither field goes into `required`.

### 4.2 Runtime behaviour

In `Agent._execute_tools` (`butterfly/core/agent.py`):

- `run_in_background=true`: call `BackgroundTaskManager.spawn(tool, kwargs, panel_dir, polling_interval)` and immediately return the placeholder `tool_result`:
  ```
  Task started. task_id=<tid>. Output will arrive in a later turn; fetch anytime with tool_output(task_id="<tid>").
  ```
- Mixed gather is safe (Q1): `asyncio.gather(...)` runs blocking and background calls together; background returns ≈ instantly.

### 4.3 Result delivery

`BackgroundTaskManager` owns a daemon-side poller (`butterfly/tool_engine/background.py`). When a spawned process completes (or stalls, or is killed), the manager:

1. Writes final state + output-file path into `sessions/<id>/core/panel/<tid>.json`.
2. Emits a `panel_update` event on `events.jsonl` (for UI).
3. **Appends a single user-role notification message** to `context.jsonl`:
   ```
   Background task <tid> (<tool>) completed with exit 0 in 47s. 2.3KB output.
   Fetch full output: tool_output(task_id="<tid>").
   ```
   Stall and kill notifications use the same one-shot pattern with different wording.
4. Wakes the daemon loop (same mechanism as user-input wake), which triggers the next agent iteration.

Append-once is critical — see §8.

### 4.4 Polling / stall watchdog semantics

- **No `polling_interval` set (default)**: only the stall watchdog runs. If 5 minutes pass with no new bytes on stdout, emit a stall notification once (re-arm only if new output then stops again).
- **`polling_interval` set**: at each tick, if new bytes accumulated since last tick, emit a `progress` notification delivering the delta (see §8 on delta semantics).
- **On completion**: final notification includes exit code, duration, total bytes. Polling ticks stop.

The watchdog also scans the tail of stdout for interactive-prompt patterns (`[y/N]`, `Press enter`, `(y/n)`, `Password:`) and surfaces them in the stall notification so the agent knows to kill or respawn.

---

## 5. Panel — in-loop work surface

`sessions/<id>/core/panel/` sits alongside `core/tasks/`. It holds per-call state for non-blocking tools and (future) sub-agent references.

### 5.1 Schema (`sessions/<id>/core/panel/<tid>.json`)

```jsonc
{
  "tid": "bg_a3f1",                    // stable id, also the filename stem
  "type": "pending_tool",              // "pending_tool" | "sub_agent" (future)
  "tool_name": "bash",
  "input": { "command": "..." },       // what agent passed

  "status": "running",                 // running | completed | stalled | killed | killed_by_restart
  "created_at": 1712345678.0,
  "started_at": 1712345678.1,
  "finished_at": null,

  "polling_interval": null,            // seconds | null (stall-watchdog only)
  "last_delivered_bytes": 0,           // stdout offset already pushed to agent
  "last_activity_at": 1712345680.4,    // last time bytes were appended to output file

  "pid": 42817,
  "exit_code": null,

  "output_file": "_sessions/<id>/tool_results/bg_a3f1.txt",
  "output_bytes": 1842,

  "meta": {}                           // free-form tool-specific fields
}
```

### 5.2 Lifecycle

- Created by `BackgroundTaskManager.spawn` → status `running`.
- Updated in place by the manager's poller as bytes arrive.
- Transitions to `completed` / `stalled` / `killed` via the manager.
- On server restart, daemon marks every `running` entry `killed_by_restart` on init and emits the corresponding notification (Q3).
- Never deleted while session exists — persistent audit trail; pruning is future work.

### 5.3 UI surfaces

- **Web**: new **Panel** tab in the right sidebar, parallel to **Tasks**. Lists entries by recency, one row per entry with status badge + first-line summary. Click-through shows full state + `Kill` / `Fetch full output` actions.
- **CLI**:
  - `butterfly panel` — one line per entry: `<tid> <tool_name> <status> <last-output-tail-one-line>`
  - `butterfly panel --tid <tid>` — full entry detail + `--kill` / `--output` subcommands

---

## 6. Structured tool outputs

All tools return strings for provider compatibility, but adopt conventions so the agent and the UI can parse them.

- **Commands (bash/session_shell)**: `<output>\n[exit N, duration T, truncated bool]` with optional `[spilled: <path>]` line if output was written to disk.
- **File tools (read)**: `<content>\n[read N bytes, lines A-B of L]`.
- **File tools (write/edit)**: `Wrote 1234 bytes to <path>.` / `Replaced 1 occurrence of '...' in <path>.`
- **Search (grep/glob)**: standard ripgrep-style line output; truncation marker at the end if result size exceeds limit.
- **Errors**: prefix `Error: ` followed by a concise message. The tool result's `is_error` flag is also set at the agent-loop layer when an exception is raised.

### 6.1 Disk spillover

Per-tool `max_result_chars`. If exceeded:
1. Write the full result to `_sessions/<id>/tool_results/<tool>_<uuid>.txt`.
2. Return the last `max_result_chars` bytes + a `[spilled: <path>]` line.
3. Agent can `read(path=...)` to retrieve the rest.

Defaults:
- `bash`: 10_000 chars
- `read`: 100_000 chars (pagination is the primary mechanism)
- `grep`: 30_000 chars
- Others: 10_000 chars

---

## 7. Memory tools (β pattern)

Starting in v2.0.5, sub-memory is **not** injected into the system prompt. Only `core/memory.md` (main) is. Full rationale and flow in `docs/butterfly/session_engine/design.md`.

- `recall_memory(name?)`: read-only. If `name` omitted, lists available sub-memories (derived from main memory's index lines). Otherwise returns full `core/memory/<name>.md`.
- `update_memory(name, old_string, new_string, description?)`: Edit-style patch on `core/memory/<name>.md` (creates the file if new), AND upserts the one-line index entry `<name>: <description>` in `core/memory.md`. `description` is required the first time a sub-memory is created; optional for subsequent edits (leaves the existing index line alone).

No free-standing `memory_write` — everything goes through `update_memory` to keep the index in sync.

---

## 8. Append-once notification lifecycle

Claude Code's prompt-time reminder injection caused context exhaustion when reminders weren't garbage-collected (issues #6854/#11716/#13249). Butterfly avoids this structurally.

- **All** notifications — background completion, stall, progress heartbeats, kill-by-restart — are appended to `context.jsonl` **exactly once**, as ordinary user-role messages.
- Nothing is re-injected at prompt build time. The normal "replay context.jsonl to rebuild prompt" path sees each notification once, forever, just like any other message.
- Growth is O(N_notifications), not O(N_notifications × N_turns).
- Progress heartbeats use **delta semantics**: each notification contains only bytes appended since the last delivery for that task, tracked via `panel/<tid>.json#last_delivered_bytes`. Re-reading the file from scratch is never forced on the agent.

The events.jsonl side also gets a mirror event per notification (for UI, not prompt), but events.jsonl is not replayed as context; it's only for live display.

---

## 9. ToolLoader context injection (unchanged architecture)

Preserved from v2.0.x: `Session._load_session_capabilities` constructs a `ToolLoader` with all context pre-bound. The loader reads `tool.md`, dynamically imports each toolhub executor, and instantiates it with the relevant context.

Per-tool context injection table (v2.0.5):

| Tool | Auto-injected |
|---|---|
| `bash` | `workdir`, `tool_results_dir` (for disk spillover) |
| `session_shell` | `workdir`, `venv_env_provider` |
| `read`/`write`/`edit` | `workdir` (for relative path resolution) |
| `glob`/`grep` | `workdir` |
| `web_search`/`web_fetch` | (provider-registry driven; no constructor injection) |
| `skill` | skills list |
| `recall_memory` | `memory_dir` |
| `update_memory` | `memory_dir`, `main_memory_path` |
| `task_*` | `tasks_dir` |
| `tool_output` | `panel_dir`, `tool_results_dir` |

`bash` itself does NOT receive `panel_dir` — the agent-loop layer owns the
routing to `BackgroundTaskManager`; the bash executor only runs the sync path.

---

## 10. Session-authored tools (unchanged)

Agents can still create `.json` + `.sh` pairs in `core/tools/`; the `.sh` script receives kwargs as JSON on stdin. Since `shell` is gone as a separate tool, these are loaded via the same generic `ToolLoader.load_local_tools` path and surface under their declared names.

---

## 11. Backward compatibility

**None.** v2.0.5 is a breaking release; `shell`, `manage_task`, `reload_capabilities` are removed. Entities (`entity/agent`, `entity/butterfly_dev`) have their `tools.md` rewritten as part of this release. Sessions created before v2.0.5 will fail to load removed tools — users must create new sessions or manually edit their session `core/tools.md`.

---

## 12. Sub-agent tool (v2.0.13)

`sub_agent` is a backgroundable tool that spawns a **child session** of the
same entity as the parent. It exists so a parent can delegate context-heavy
work (research, sandboxed experiments, large refactors) without polluting
its own conversation history.

### Semantics

- **The parent only ever sees the child's FINAL reply.** Intermediate tool
  calls, partial messages, and thinking blocks stay in the child session
  (visible via the sidebar / panel). This is a hard contract — the
  description in `toolhub/sub_agent/tool.json` and the mode prompts state
  it explicitly so the LLM doesn't expect a transcript.
- Sync mode (`run_in_background=false`, default): the parent's turn blocks
  until the child replies or `timeout_seconds` elapses. On timeout, the
  child keeps running.
- Background mode (`run_in_background=true`): identical to bash bg —
  parent gets a `task_id=…` placeholder immediately and continues; the
  child's completion arrives later as a `user_input` notification appended
  to the parent's `context.jsonl` (with the child's full reply inline).

### Modes

| Mode | Permission | Use when |
|---|---|---|
| `explorer` | Sandboxed: writes only inside child's `playground/`. Reads anywhere. Bash cwd pinned to playground. | Research, untrusted exploration, parallel investigations. |
| `executor` | No sandbox. Same tool surface as parent. | The child legitimately needs to modify shared files. |

The mode prompt (`toolhub/sub_agent/<mode>.md`) is copied to the child's
`core/mode.md` at `init_session` time and folded into the child's static
system prompt by `Session._load_session_capabilities` (between `system.md`
and `env.md`).

### Implementation split

- `butterfly/tool_engine/sub_agent.py` — both `SubAgentTool` (sync executor)
  and `SubAgentRunner` (background runner). Shared helper `_spawn_child`
  factors out the `init_session(...)` call so both paths agree on
  child-id format, manifest layout, and message composition.
- `toolhub/sub_agent/executor.py` — re-exports the canonical classes so
  the `ToolLoader` can find `SubAgentTool` via the conventional discovery
  path.
- `toolhub/sub_agent/{tool.json,explorer.md,executor.md}` — schema +
  mode prompts.

### Parent-side observability

When the runner is in background mode it owns a `PanelEntry` of type
`sub_agent` (constant `panel.TYPE_SUB_AGENT`). The runner stamps:

- `meta.child_session_id` — for sidebar pivot + "Open child session" link.
- `meta.mode` — for the mode chip.
- `meta.last_child_state` — refreshed every `polling_interval` seconds by
  tailing the child's `events.jsonl`. Drives the `tool_progress` event
  the parent's chat UI uses to keep the tool cell yellow with a live
  summary.
- `meta.result` / `meta.result_text` — populated on completion.

Web UI (see `ui/web/frontend/src/components/{chat,sidebar,panel}.ts`):

- Chat HUD shows `⚙ N sub-agents running` while any sub_agent panel
  entry is non-terminal.
- The chat-side tool cell stays yellow ("working") until the matching
  `tool_finalize` event arrives (also fixes the prior bash-bg bug where
  the cell flashed green immediately on the spawn placeholder).
- The parent's panel renders the sub_agent card with a thumbnail
  (current activity) and an expandable view of the child's last 5
  events (via `GET /api/sessions/{child_id}/events_tail?n=5`).
- The sidebar indents the child session under its parent (markdown-list
  style) keyed off the new `parent_session_id` field in `manifest.json`.

---

## 13. Generalized BackgroundTaskManager (v2.0.13)

v2.0.5 introduced `BackgroundTaskManager` for bash. v2.0.13 splits the
manager into two orthogonal halves so any backgroundable tool can plug in:

- **The manager** owns: tid generation, `PanelEntry` lifecycle, the
  `BackgroundEvent` queue, `sweep_restart` for orphan recovery, and the
  terminal-event emission contract.
- **A `BackgroundRunner`** owns: "what does this tool actually do in the
  background?" Plus per-tool `validate(input)` (run synchronously at
  `spawn()` time so misconfiguration surfaces immediately), `run(ctx, tid,
  entry, input, polling_interval)`, and `kill(ctx, tid)`.

Bash is registered automatically as the default runner (`BashRunner`); it
implements the same subprocess + drain logic that lived inline before.
Sub_agent registers `SubAgentRunner` from `Session.__init__`. New
backgroundable tools can register their own runner without touching the
manager.

`spawn(tool_name, input, polling_interval)` defaults `entry_type` to
`TYPE_SUB_AGENT` when `tool_name == "sub_agent"`, else `TYPE_PENDING_TOOL`.
This lets the UI render the two card types differently without runners
having to know about panel taxonomy.
