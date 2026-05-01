# CLI — dispatch over `butterfly.runtime.io`

The CLI is the thinnest possible shell over `butterfly.runtime.io`: every command is either a reflection into an `io.*` function or a small argparse wrapper that calls exactly one. No business logic, no alternate file IO. If the web UI can do something, the CLI can too — and vice versa.

## Generic reflection — `butterfly io`

```
butterfly io <function> [<pos>...] [--kw=value...]
butterfly io list                      # enumerate every io.* function with signature
butterfly io                           # same as `io list`
```

Reflects into `butterfly.runtime.io.<function>`. Positional args become positionals; `--name=value` becomes keyword args. Values parse as JSON when possible (`--since_id=10` → int 10; `--text=hello` → str `"hello"` since `hello` isn't valid JSON; `--enabled=true` → bool `True`). The result is coerced through `to_dict` / `dataclasses.asdict` / iterator materialisation and printed as JSON.

Examples:

```
butterfly io list_sessions --json
butterfly io read_llm_context session_abc123
butterfly io send_message session_abc123 --text="hello"
butterfly io upsert_task session_abc123 test --description="hi"
butterfly io interrupt_session session_abc123 --text="stop please"
```

Every function in `runtime/io.py` is automatically reachable. Adding an `io.*` function makes it CLI-callable with zero extra wiring. Error mapping:

| Exception | Exit code | Message |
|---|---|---|
| unknown function, `--help`-like | 2 | lists catalog on stderr |
| wrong arity / unknown kwarg (`TypeError`) | 2 | prints `signature:` hint |
| `FileNotFoundError` / `ValueError` / `IOError` / `NotImplementedError` | 2 | `Error: <exc>` |

## Friendly aliases

Shortcuts for the writes and reads an operator types most often. Each is a thin argparse wrapper around one `io.*` call — pretty-printing lives next to the handler that needs it:

```
butterfly interrupt <sid> [--text=T]          # io.interrupt_session
butterfly delete <sid> --yes                  # io.delete_session (guarded)
butterfly task-upsert <sid> <name> [...]      # io.upsert_task
butterfly task-delete <sid> <name> --yes      # io.delete_task (guarded)
butterfly shell <sid> <text>                  # io.terminal_input
butterfly config-set <sid> <key> <value>      # io.update_config (value JSON-parsed)
butterfly prompt-edit <sid> <name>            # read_prompt → $EDITOR → update_prompt
```

Destructive aliases (`delete`, `task-delete`) refuse to run without `--yes` to prevent accidental shell globbing.

The `chat` / `new` / `stop` / `start` / `sessions` / `log` / `tasks` subcommands in `ui/cli/main.py` keep their pretty UX (tables, `--inject-memory`, `--no-wait`, tail mode) and internally call through `runtime.io`. The equivalent reflection calls (`butterfly io send_message …`, `butterfly io create_session …`) work too.

## Server commands

`butterfly` (no args) starts the web server + UI and hangs. `butterfly server` tails the running server's log. These are not `runtime.io` reflections — they manage the uvicorn process lifecycle.

## Invariant

I6 from the refactor: every function in `butterfly.runtime.io` is exercisable from the CLI. Adding a writer without a CLI surface is a bug; the `butterfly io` reflection covers it for free, so the check reduces to "is the new function in `io.py` at all?"
