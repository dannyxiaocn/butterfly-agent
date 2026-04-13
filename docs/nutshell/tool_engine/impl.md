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
from nutshell.tool_engine.loader import ToolLoader

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
- `bash` auto-activates session `.venv` when `NUTSHELL_SESSION_ID` is set
- `web_search` backend switchable via `config.yaml` → `tool_providers.web_search`
- `manage_task` actions: `create`, `update`, `pause`, `resume`, `finish`, `list`; `pause` is user-initiated stop, `resume` returns to `pending`
- `manage_task` and `recall_memory` have path traversal protection
