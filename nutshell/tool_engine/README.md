# `nutshell/tool_engine`

This subsystem turns filesystem tool definitions into executable `Tool` objects and hosts the built-in tool implementations.

## What This Part Is

- `loader.py`: loads `*.json` schemas from `core/tools/` and resolves an implementation.
- `registry.py`: built-in tool lookup and provider swapping for tools such as `web_search`.
- `reload.py`: creates the `reload_capabilities` tool that `Session` injects automatically.
- `executor/`: concrete executors for terminal tools, the `skill` tool, and web-search backends.
- `sandbox.py`: currently an empty placeholder.

## Tool Sources

| Source | Example | How It Runs |
| --- | --- | --- |
| Built-in | `bash`, `skill`, `web_search` | Python executor |
| Session tool | `core/tools/foo.json` + `foo.sh` | `ShellExecutor` |
| Injected meta tool | `reload_capabilities` | created directly by `Session` |

## How To Use It

Load a directory of tool definitions:

```python
from pathlib import Path
from nutshell.tool_engine import ToolLoader

tools = ToolLoader(default_workdir=".").load_dir(Path("core/tools"))
```

Create a new session-scoped tool:

1. add `core/tools/<name>.json`
2. add `core/tools/<name>.sh`
3. make the script executable
4. call `reload_capabilities`

## How It Contributes To The Whole System

- `session_engine.Session` reloads tools from disk before each activation.
- `core.Agent` sees only normalized `Tool` objects and does not care whether a tool is built-in or shell-backed.
- The base entity exposes built-ins by shipping JSON schemas in `entity/agent/tools/`.

## Important Behavior

- Resolution order is: explicit registry override -> `skill` executor -> `bash` executor -> sibling `.sh` script -> built-in registry -> stub.
- `bash` runs from the session directory by default, so agents can use short relative paths like `core/tasks/`.
- `bash` auto-activates the session `.venv` when `NUTSHELL_SESSION_ID` is set.
- `web_search` can switch backend through `params.json` with `tool_providers.web_search`.

Executor details live in [executor/README.md](/Users/xiaobocheng/agent_core/nutshell/nutshell/tool_engine/executor/README.md).

