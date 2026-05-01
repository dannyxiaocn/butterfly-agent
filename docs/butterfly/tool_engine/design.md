# Tool Engine — Design

The tool engine turns tool definitions into executable `Tool` objects. Tools live in `toolhub/` (centralized, repo-wide) and are enabled per-agent via `tools.md` (one tool name per line). The `ToolLoader` dynamically imports executors at session init and injects session context (workdir, tasks_dir, memory_dir, etc.) into their constructors so agents never pass environmental parameters.

---

## 1. Design principles

1. **One tool, one job.** No dispatcher tools. Verb-named tools beat `action:` strings.
2. **Minimal parameters.** Every deterministic-per-session value is injected at ToolLoader time, not by the agent per call.
3. **Schema + description define agent UX.** Both are authoritative.
4. **Structured results.** Tools return strings, but those strings are conventionalized (see §6). Large outputs spill to disk.
5. **Append-only history.** Notifications and events are appended to `events_v1.jsonl` exactly once — never re-injected at prompt-build time. The LLM context is rebuilt from `events_v1.jsonl` via `build_llm_context()` at every tick (see [`docs/butterfly/runtime/events.md`](../runtime/events.md)).

Built-in tools follow a `<component>_<action>` naming convention (`memory_recall`, `task_create`, `task_list`, …).

---

## 2. Tool catalog

Toolhub-declared tools (`toolhub/<name>/`) plus session-authored `.json`+`.sh` pairs.

| Name | Purpose | Backgroundable | Agent sees |
|---|---|---|---|
| `bash` | One-shot shell command, fresh process every call | **Yes** | `command, timeout?, stdin?, run_in_background?, polling_interval?` |
| `sub_agent` | Spawn a child session (same agent), return its FINAL reply | **Yes** | `name, task, mode (explorer\|executor), timeout_seconds?, run_in_background?, polling_interval?` |
| `terminal_create` | Open the session's persistent pty terminal (idempotent). Returns the welcome block `[terminal ready]\nenv: …\npath: …\ngit: …`. | No | — |
| `terminal_use` | Run one command against the terminal created by `terminal_create`. Fails closed when called before create. Prepends `[env: … \| path: … \| git: …]` when the fingerprint changes. | No | `command, timeout?, idle_threshold?` |
| `read` / `write` / `edit` | File reads, writes, exact-string replacement | No | path-shaped args |
| `glob` / `grep` | Pattern + content search | No | pattern args |
| `web_search` / `web_fetch` | Multi-provider search + URL fetch | No | query / url |
| `skill` | Load a SKILL.md into context | No | `skill, args?` |
| `memory_recall` / `memory_update` | Read full sub-memory file / Edit sub-memory + main-memory index line | No | name + edit args |
| `task_create` / `task_update` / `task_finish` / `task_pause` / `task_resume` / `task_list` | Bash-driven task card lifecycle (single script, last line `[skip]`/`[start]`/`[done]`) | No | name + fields |
| `tool_output` | Fetch full output of a backgrounded tool call | No | `task_id, delta?` |
| `workflow` | Run an ordered pipeline of sub-agent steps; previous step's reply substitutes for `{prev}`. See [workflow.md](workflow.md). | **Yes** | `steps[{name, task, agent?, mode?}], run_in_background?, polling_interval?` |
| `teamchat_send` / `teamchat_view` | Group chat in a `kind: team` session. Auto-injected on member sessions. See [agent_team.md](../session_engine/agent_team.md). | No | `text` / — |

---

## 3. Bash (one-shot) vs terminal_create + terminal_use (persistent)

### 3.1 `bash` — stateless one-shot

- Every call spawns a fresh subprocess via `asyncio.create_subprocess_shell`. `cd` / `export` / aliases **do not persist** across calls.
- Auto-injected `workdir` = session directory (agent uses relative paths). Session venv (`sessions/<id>/.venv`) is auto-activated via env injection if present.
- No PTY. Output is clean bytes; stderr merged with stdout via `2>&1` inside the command if separation is needed.
- Optional `stdin: str` parameter pipes to the process for pre-feeding interactive prompts.
- Backgroundable (see §4).

Output shape: `<stdout/stderr combined>\n[exit N, duration 2.3s, truncated false]`. Output > `max_output_chars` (default 10_000) tail-truncates and appends `[spilled: _sessions/<id>/tool_results/<uuid>.txt]`; `tool_output` or `read` fetches the full file.

### 3.2 `terminal_create` + `terminal_use` — persistent

- One long-lived `bash --norc --noprofile` per session, attached to a pseudo-tty. `terminal_create` spawns it (idempotent); `terminal_use` runs one command at a time against it.
- Fail-closed ordering: `terminal_use` before `terminal_create` returns `Error: Terminal not created, please use terminal_create tool to create one first`.
- Sentinel protocol: each `terminal_use` writes `{command}\nprintf '\n__BFY_DONE_<marker>_%d__\n' $?\n` and reads until the marker; the exit code embeds in the marker.
- Env fingerprint: both verbs probe `(venv, cwd, git_branch)` inside the pty. `terminal_create` returns the snapshot as a welcome block; `terminal_use` prepends `[env: <venv> | path: <cwd> | git: <branch>]` when the fingerprint changes since the last call.
- Workdir, env vars, aliases, functions persist between calls.
- Single-command timeout sends SIGINT through the pty; the shell stays alive (next `terminal_create` does a hard respawn if it's wedged).
- Auto-restart if the shell dies between calls; next output is prefixed `[shell restarted]`.
- Not backgroundable — long-running work goes in `bash` with `run_in_background=true`. The terminal is for sequencing, not background processes.
- No parallel calls within one session; the lock is enforced inside the executor.

Implementation detail in [pure_context.md](pure_context.md).

### 3.3 When to use which

The tool descriptions explicitly point at each other:

- `bash` → "one-shot, fresh process; for multi-step workflows that need to share environment, use `terminal_create` + `terminal_use` instead."
- `terminal_create` → "open the session's persistent pty terminal; call this ONCE before the first `terminal_use`."
- `terminal_use` → "run a command inside the persistent pty terminal; not for long-running background processes — use `bash(run_in_background=true)`."

---

## 4. Backgroundable tools (non-blocking execution)

Uniform opt-in non-blocking protocol. Tools whose `tool.json` sets `"backgroundable": true` participate; today: `bash`, `sub_agent`, `workflow`.

### 4.1 Protocol

When `Tool.backgroundable == True`, `ToolLoader` automatically:

1. Adds `run_in_background: bool` and `polling_interval: int|null` to the tool's schema (neither required).
2. Appends a standard paragraph to the tool description explaining the placeholder result, deferred delivery, `tool_output(task_id)`, the 5-minute stall watchdog, and panel visibility.

### 4.2 Runtime behaviour

In `Agent._execute_tools` (`butterfly/core/agent.py`):

- `run_in_background=true` → `BackgroundTaskManager.spawn(tool, kwargs, panel_dir, polling_interval)` returns immediately with a placeholder result `Task started. task_id=<tid>. ...`.
- `asyncio.gather` mixes blocking and background calls safely; background returns ≈ instantly.

### 4.3 Result delivery

`BackgroundTaskManager` (`butterfly/tool_engine/background.py`). On completion / stall / kill:

1. Writes final state + output-file path into `sessions/<id>/core/panel/<tid>.json`.
2. Appends `panel_entry_changed` to `events_v1.jsonl`.
3. Appends a single `user_input` event (`source="task"` / `source="sub_agent"` / etc.) with the completion message + `tool_output` hint.
4. Wakes the daemon loop, which triggers the next agent iteration.

Append-once is critical (see §8).

### 4.4 Polling / stall watchdog

- No `polling_interval` → only the stall watchdog runs (5 min of no new bytes ⇒ one-shot stall notification, re-arms only on next stall).
- `polling_interval` set → progress notification per tick with bytes-since-last-tick (delta semantics).
- Completion notification carries exit code, duration, total bytes.

The watchdog tail-scans for interactive-prompt patterns (`[y/N]`, `Press enter`, `(y/n)`, `Password:`) and surfaces them in the stall notification.

---

## 5. Panel — in-loop work surface

`sessions/<id>/core/panel/<tid>.json` per backgroundable call. Schema (excerpt):

```jsonc
{
  "tid": "bg_a3f1",
  "type": "pending_tool",          // "pending_tool" | "sub_agent"
  "tool_name": "bash",
  "input": { ... },
  "status": "running",             // running | completed | stalled | killed | killed_by_restart
  "polling_interval": null,
  "last_delivered_bytes": 0,
  "last_activity_at": ...,
  "pid": 42817,
  "exit_code": null,
  "output_file": "_sessions/<id>/tool_results/bg_a3f1.txt",
  "output_bytes": ...,
  "meta": {}                       // free-form
}
```

Lifecycle: created by `spawn` → updated in place by the manager's poller → transitions to terminal status via the manager. On daemon restart, every `running` entry is marked `killed_by_restart` with a corresponding notification.

UI surfaces: web sidebar Panel tab + CLI `butterfly panel`.

---

## 6. Structured tool outputs

All tools return strings, but adopt conventions for parsing.

- Commands (bash / terminal_use): `<output>\n[exit N, duration T, truncated bool]` with optional `[spilled: <path>]`.
- File reads: `<content>\n[read N bytes, lines A-B of L]`.
- Writes / edits: `Wrote N bytes to <path>.` / `Replaced N occurrence(s) of '...' in <path>.`
- Search: ripgrep-style line output; truncation marker if size limit exceeded.
- Errors: `Error: <message>` prefix; the agent loop also stamps `is_error` on the tool-result event.

### 6.1 Error classification

Some tools complete without raising but encode failure in the body (`bash` returning `[exit 127, ...]`, executor echoing a `Traceback`). `butterfly/tool_engine/result_classifier.py::classify_tool_result(tool_name, result) -> bool` is called from `core/agent.py::_execute_tools` after `tool.execute()`. The flag combines with the exception-path `is_error` and is stamped onto the `agent_tool_result` event.

Per-tool rules live at the top of `result_classifier.py`. `bash` and `terminal_use` parse the last `[exit N, ...]` footer (last match wins); `[timed out after ...]` is always an error. `terminal_create` falls through to default (welcome-block has no footer). Default rule: `Traceback (most recent call last):` anywhere, or first non-empty line starting with `Error:` / `ERROR:` / `Error ` / `Traceback…`.

The classifier errs on the side of green — unknown tools get false negatives, never false positives. Add a rule when a tool's failure idiom slips past the default.

The web reducer keys off `tool_use_id` to upgrade the matching tool Card to its error styling (live SSE and history replay drive the same reducer; see [`docs/ui/web/design.md`](../../ui/web/design.md)).

### 6.2 Disk spillover

Per-tool `max_result_chars`. If exceeded: write full result to `_sessions/<id>/tool_results/<tool>_<uuid>.txt`, return last `max_result_chars` + `[spilled: <path>]` line. Defaults: `bash` 10_000, `read` 100_000, `grep` 30_000, others 10_000.

---

## 7. Memory tools

Sub-memory is **not** injected into the system prompt — only `core/memory.md` (the index) is. Full rationale in `docs/butterfly/session_engine/design.md`.

- `memory_recall(name?)`: read-only. Omit `name` to list available sub-memories (parsed from main-memory index lines); else return full `core/memory/<name>.md`.
- `memory_update(name, old_string, new_string, description?)`: Edit-style patch on the sub-memory file (creates if new), AND upserts the one-line index entry `<name>: <description>` in `core/memory.md`. `description` required on first creation; optional thereafter.

No free-standing `memory_write` — everything goes through `memory_update` to keep the index in sync.

---

## 8. Append-once notification lifecycle

Claude Code's prompt-time reminder injection caused context exhaustion when reminders weren't garbage-collected. Butterfly avoids this structurally.

- All notifications — background completion, stall, progress heartbeats, kill-by-restart — are appended to `events_v1.jsonl` exactly once, as ordinary `user_input` events (`source` distinguishes background-task vs. sub-agent vs. operator).
- Nothing is re-injected at prompt-build time. `build_llm_context()` is a pure filter over the event log.
- Growth is O(N_notifications), not O(N_notifications × N_turns).
- Progress heartbeats use delta semantics: each notification carries only bytes appended since the last delivery for that task, tracked via `panel/<tid>.json#last_delivered_bytes`.

UI-only system events (`panel_entry_changed`, `tool_progress`) carry `for_llm=False` in the same log; the LLM context builder filters them out, the web reducer renders them.

---

## 9. ToolLoader context injection

`Session._load_session_capabilities` constructs a `ToolLoader` with all context pre-bound. The loader reads `tools.md`, dynamically imports each toolhub executor, and instantiates it with the relevant context.

| Tool | Auto-injected |
|---|---|
| `bash` | `workdir`, `tool_results_dir` |
| `terminal_create` / `terminal_use` | `workdir`, `venv_env_provider`, `terminal_logger` (shared `TerminalExecutor`) |
| `read` / `write` / `edit` | `workdir` |
| `glob` / `grep` | `workdir` |
| `web_search` / `web_fetch` | (provider-registry driven; no constructor injection) |
| `skill` | skills list |
| `memory_recall` / `memory_update` | `memory_dir` (+ `main_memory_path` for update) |
| `task_*` | `tasks_dir` |
| `tool_output` | `panel_dir`, `tool_results_dir` |

`bash` does NOT receive `panel_dir` — the agent-loop layer owns the routing to `BackgroundTaskManager`; the bash executor only runs the sync path.

---

## 10. Session-authored tools

Agents can create `.json` + `.sh` pairs in `core/tools/`; the `.sh` script receives kwargs as JSON on stdin. `ToolLoader.load_local_tools` surfaces them under their declared names.

---

## 11. Sub-agent tool

`sub_agent` is a backgroundable tool that spawns a child session of the same agent as the parent. It exists so a parent can delegate context-heavy work (research, sandboxed experiments, large refactors) without polluting its own conversation history.

### Semantics

- **The parent only ever sees the child's FINAL reply.** Intermediate tool calls, partial messages, and thinking blocks stay in the child session (visible via the sidebar / panel). The tool description and mode prompts state this explicitly.
- Sync mode (`run_in_background=false`, default): parent's turn blocks until the child replies or `timeout_seconds` elapses. On timeout, the child keeps running — its final reply is delivered later via the background-completion path.
- Background mode: identical to bash bg — parent gets a `task_id=…` placeholder immediately and continues; the child's completion arrives later as a `user_input` event (`source="sub_agent"`) with the full reply inline.

### `name` parameter

Every `sub_agent` call must supply a short human-readable `name` (≤ 40 chars). It threads through `_spawn_child` → `init_session(display_name=…)` → child manifest's `display_name` → parent's `PanelEntry.meta.display_name`. The sidebar and panel prefer `display_name` over the raw `session_id` (which stays the canonical unique key).

### Modes

| Mode | Permission | Use when |
|---|---|---|
| `explorer` | Sandboxed (Guardian): writes only inside child's `playground/`. Reads anywhere. Bash cwd pinned to playground. | Research, untrusted exploration, parallel investigations. |
| `executor` | No sandbox. Same tool surface as parent. | Child legitimately needs to modify shared files. |

Mode prompt (`toolhub/sub_agent/<mode>.md`) is copied to child's `core/mode.md` at `init_session` time and folded into the static system prompt by `Session._load_session_capabilities`.

### Cancel cascade

A child session's daemon runs independently of the parent's await chain. When the parent cancels mid-`sub_agent` (chat-with-mode=interrupt, ⚡ Interrupt button, or Stop), the asyncio cancel propagating up through `_execute_tools → SubAgentTool.execute()` does **not** reach the child daemon — without explicit cascade, the child keeps spending tokens. Two cooperating fixes:

- `SubAgentTool.execute` (blocking path) wraps `await _wait_for_reply` in `try/except CancelledError` that calls `BridgeSession(child).send_interrupt()` before re-raising.
- `SubAgentRunner.kill` (background path) calls `send_interrupt()` first, then `stop_session()` — without the interrupt, the daemon's stopped-check only fires when fresh input arrives, leaving in-flight chats running.

The bare-interrupt cascade in `Session._handle_explicit_interrupt` reaches background sub-agents via `_cascade_interrupt_background()` → `BackgroundTaskManager.kill(tid)` → `SubAgentRunner.kill`. Blocking sub-agents are reached through the parent's `_run_task.cancel()` propagating naturally to the awaited `SubAgentTool.execute()`.

### Implementation split

- `butterfly/tool_engine/sub_agent.py` — `SubAgentTool` (sync executor) + `SubAgentRunner` (background runner). Shared helper `_spawn_child` factors out `init_session(...)`.
- `toolhub/sub_agent/executor.py` — re-exports for `ToolLoader` discovery.
- `toolhub/sub_agent/{tool.json, explorer.md, executor.md}` — schema + mode prompts.

### Parent-side observability

In background mode the runner owns a `PanelEntry` of type `sub_agent`. The runner stamps:

- `meta.child_session_id` — for sidebar pivot + "Open child session" link.
- `meta.mode` — for the mode chip.
- `meta.last_child_state` — refreshed every `polling_interval` seconds by tailing the child's `events_v1.jsonl`. Drives the `tool_progress` event the parent's chat UI uses to keep the tool Card in its "running" state.
- `meta.result` / `meta.result_text` — populated on completion.

Web UI: chat HUD shows `⚙ N sub-agents running` while any sub_agent panel entry is non-terminal; chat-side tool Card stays in "running" state until the matching `agent_tool_result` arrives (the unified reducer keys by `tool_use_id`); sidebar indents children under their parent via `parent_session_id` in `manifest.json`.

---

## 12. BackgroundTaskManager — runner registry

The manager owns: tid generation, `PanelEntry` lifecycle, the `BackgroundEvent` queue, `sweep_restart` for orphan recovery, and the terminal-event emission contract.

A `BackgroundRunner` owns the per-tool work: `validate(input)` (synchronous at `spawn()` time), `run(ctx, tid, entry, input, polling_interval)`, and `kill(ctx, tid)`.

Bash registers `BashRunner` as the default. `Session.__init__` registers `SubAgentRunner` and `WorkflowRunner`. New backgroundable tools register their own runner without touching the manager.

`spawn(tool_name, input, polling_interval)` defaults `entry_type` to `TYPE_SUB_AGENT` when `tool_name == "sub_agent"`, else `TYPE_PENDING_TOOL`. UI renders the two card types differently.

---

## 13. Provider-native built-in tools

A second tool kind: **provider-native built-in tools** declared to the provider at request time but executed server-side (currently Codex / OpenAI Responses). Function tools and built-in tools share the `Tool` object type; what differs is how providers handle them.

| Kind | When | Examples |
|---|---|---|
| Function tool | Butterfly owns the executor; works on every provider. | `bash`, `read`, `task_create`, `web_search_brave`. |
| Provider-native built-in | Leverage the provider's server-side capability. Works only on providers that understand the specific tool type. | `web_search`, `file_search`, `code_interpreter`. |

Prefer a function tool unless the provider-native one offers a capability you can't get locally.

### `builtin_dict` plumbing

`butterfly/core/tool.py::Tool` accepts `builtin_dict: dict | None` at construction. When set, `to_builtin_dict()` returns a copy and `is_builtin` is True. Providers that support built-in tools call `to_builtin_dict()` first when formatting `tools=[]` — a non-None return is spliced verbatim (as `{"type": "web_search"}`, etc.), NOT wrapped as `type: "function"`.

Direct invocation of a built-in tool's `execute()` raises `NotImplementedError` — surfacing the config mistake (e.g. enabling `web_search` against a Kimi provider) early.

### Registration in toolhub

A provider-native built-in tool entry under `toolhub/<name>/`:

1. `tool.json` — same shape as a function tool. `input_schema` can be empty since the agent doesn't supply parameters.
2. `executor.py` — declare a module-level class attr `builtin_dict` on the executor class with the raw provider spec:
   ```python
   class WebSearchExecutor:
       builtin_dict = {"type": "web_search"}
       async def execute(self, **_) -> str:
           raise NotImplementedError("provider-native built-in tool — ...")
   ```
3. `butterfly/tool_engine/loader.py::_load_builtin_dict` reads the class attr (best-effort — absent ⇒ `None` ⇒ regular function tool).

Three stubs ship: `toolhub/web_search/`, `toolhub/file_search/`, `toolhub/code_interpreter/`.

The provider exposes `consume_builtin_tool_events()` to drain progress events (`web_search.searching`, `code_interpreter_call.code.delta`, …) for UI / telemetry. Not yet wired into chat-side `tool_progress` rendering.
