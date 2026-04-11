# Tool Engine — Design

The tool engine turns filesystem tool definitions into executable `Tool` objects and hosts built-in tool implementations.

## Design Principles

- Tools are defined as JSON schemas in `core/tools/` — the runtime resolves implementations
- Built-in tools (`bash`, `skill`, `web_search`) have Python executors
- Session-authored tools use `.json` + `.sh` pairs with `ShellExecutor`
- `reload_capabilities` is always injected by `Session` and cannot be overridden

## Tool Resolution Order

1. Explicit registry override
2. `skill` executor
3. `bash` executor
4. Sibling `.sh` script
5. Built-in registry
6. Stub
