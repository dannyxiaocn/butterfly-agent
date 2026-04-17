# Tool Engine — Implementation

## Files

| File | Purpose |
|------|---------|
| `loader.py` | ToolLoader: reads `tool.md`, imports toolhub executors, loads local `.json+.sh` tools |
| `registry.py` | Provider swap for multi-provider tools (e.g., `web_search` → brave/tavily) |
| `reload.py` | Creates the `reload_capabilities` tool injected by `Session` |
| `sandbox.py` | Placeholder for tool sandboxing |
| `executor/` | Legacy executors (ShellExecutor, etc.) |

## ToolHub Layout

```
toolhub/
├── bash/          — BashExecutor (subprocess + PTY modes)
├── web_search/    — WebSearchExecutor → delegates to brave.py or tavily.py
├── skill/         — SkillExecutor (load + render SKILL.md)
├── manage_task/   — ManageTaskExecutor (create/update/pause/resume/finish/list task cards)
└── recall_memory/ — RecallMemoryExecutor (read memory layer files)
```

Each tool: `tool.json` (schema) + `executor.py` (implementation).

## Tool Loading Flow

```
Session._load_session_capabilities()
  → ToolLoader(workdir, skills, tasks_dir, memory_dir)   # context injection
    → load_from_tool_md(core/tool.md)                     # read enabled tool names
      → load_from_toolhub(name)                           # import executor, bind context
    → load_local_tools(core/tools/)                       # agent-created .json+.sh pairs
  → resolve_tool_impl() for provider overrides            # e.g., web_search → tavily
  → inject reload_capabilities                            # always present
```

## Tool Sources

| Source | Example | How It Runs |
|--------|---------|-------------|
| ToolHub | `bash`, `manage_task`, `recall_memory` | Python executor with context injection |
| Session tool | `core/tools/foo.json` + `foo.sh` | `ShellExecutor` via stdin/stdout |
| Injected | `reload_capabilities` | Created directly by `Session` |

## Usage

```python
from butterfly.tool_engine.loader import ToolLoader

loader = ToolLoader(
    default_workdir="/path/to/session",
    tasks_dir=Path("core/tasks"),
    memory_dir=Path("core/memory"),
)
tools = loader.load_from_tool_md(Path("core/tool.md"))
```

## Creating a Session-Scoped Tool

1. Add `core/tools/<name>.json` (schema)
2. Add `core/tools/<name>.sh` (implementation, receives JSON on stdin)
3. Make the script executable
4. Call `reload_capabilities`

## Important Behaviors

- `bash` runs from the session directory by default; agent can override with `workdir`
- `bash` auto-activates session `.venv` when `BUTTERFLY_SESSION_ID` is set
- `web_search` backend switchable via `config.yaml` → `tool_providers.web_search`
- `manage_task` actions: `create`, `update`, `pause`, `resume`, `finish`, `list`; `pause` is user-initiated stop, `resume` returns to `pending`
- `manage_task` and `recall_memory` have path traversal protection

## v2.0.13 — Sub-agent + generalized background runners

- `butterfly/tool_engine/background.py` now splits into `BackgroundTaskManager`
  (scheduler / panel / events / lifecycle) and the `BackgroundRunner` protocol
  (per-tool `validate` / `run` / `kill`). `register_runner(name, runner)` wires
  a tool-specific runner; `spawn()` looks it up + calls `validate()`
  synchronously so misconfig surfaces immediately.
- `BashRunner` is auto-registered on manager construction; existing bash
  background behaviour is unchanged.
- `butterfly/tool_engine/sub_agent.py` defines `SubAgentTool` (sync path via
  `ToolLoader`) and `SubAgentRunner` (background path via
  `BackgroundTaskManager`). Both share `_spawn_child` which calls
  `init_session(..., parent_session_id, mode, sub_agent_depth=parent+1)`.
  Depth is capped at `_MAX_SUB_AGENT_DEPTH` (2) to prevent runaway forks.
- `BackgroundTaskManager.spawn(tool_name="sub_agent", …)` auto-applies
  `entry_type=TYPE_SUB_AGENT`; other tools get `TYPE_PENDING_TOOL`. See
  `butterfly/session_engine/panel.py::VALID_ENTRY_TYPES` for the allowed set.
- `toolhub/sub_agent/executor.py` re-exports the canonical classes so the
  `ToolLoader`'s conventional discovery path keeps working.
- `ToolLoader` gained `guardian` + `parent_session_id` + `sessions_base` +
  `system_sessions_base` + `entity_base` kwargs so Write/Edit/Bash receive the
  `Guardian` boundary and `SubAgentTool` receives the base paths it needs to
  call `init_session`.
- `butterfly/core/guardian.py` — `Guardian.check_write(path)` raises
  `PermissionError` if the resolved target (symlinks resolved) escapes the
  root. Write/Edit surface it as `Error: Failed to {write,edit} <path>: …`;
  bash sets subprocess cwd to the root and exports `BUTTERFLY_GUARDIAN_ROOT`.

### Guardian coverage on every shell path (PR #28 round 2 review)

The Guardian is wired through *all four* surfaces an agent can shell out:

| Surface | Wiring point |
|---|---|
| `bash` (inline) | `BashExecutor(workdir=…, guardian=…)` — pins cwd, exports env. |
| `bash` (background, `run_in_background=true`) | `BashRunner.run` reads `ctx.guardian` from `BackgroundContext`; same pin + env logic. The `BackgroundTaskManager` accepts `guardian=` and threads it into the shared context. |
| `session_shell` (persistent shell) | `SessionShellExecutor(workdir=…, guardian=…)` — Guardian overrides workdir at construction so the long-lived shell can never spawn outside the boundary; `_build_env` injects `BUTTERFLY_GUARDIAN_ROOT`. |
| `write` / `edit` | Hard `Guardian.check_write(path)` returning `Error: Failed to …: guardian: …` on violation. |

Without these the explorer-mode contract was a sieve — `session_shell`
or `bash + run_in_background` would let a child spawn a shell with cwd
anywhere on disk. Each is exercised by
`tests/butterfly/tool_engine/test_pr28_review_round2.py`.
