# Tool Engine — Implementation

## Files

| File | Purpose |
|------|---------|
| `loader.py` | Loads `*.json` schemas from `core/tools/`, resolves implementations |
| `registry.py` | Built-in tool lookup and provider swapping (e.g., `web_search` backend) |
| `reload.py` | Creates the `reload_capabilities` tool injected by `Session` |
| `sandbox.py` | Placeholder for tool sandboxing |
| `executor/` | Concrete executors (see subdirectory docs) |

## Tool Sources

| Source | Example | How It Runs |
|--------|---------|-------------|
| Built-in | `bash`, `skill`, `web_search` | Python executor |
| Session tool | `core/tools/foo.json` + `foo.sh` | `ShellExecutor` |
| Injected meta | `reload_capabilities` | Created directly by `Session` |

## Usage

```python
from pathlib import Path
from nutshell.tool_engine import ToolLoader

tools = ToolLoader(default_workdir=".").load_dir(Path("core/tools"))
```

## Creating a Session-Scoped Tool

1. Add `core/tools/<name>.json` (schema)
2. Add `core/tools/<name>.sh` (implementation)
3. Make the script executable
4. Call `reload_capabilities`

## Important Behaviors

- `bash` runs from the session directory by default
- `bash` auto-activates session `.venv` when `NUTSHELL_SESSION_ID` is set
- `web_search` backend switchable via `params.json` → `tool_providers.web_search`
