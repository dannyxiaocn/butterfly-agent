# CLI — Implementation

## Files

| File | Purpose |
|------|---------|
| `main.py` | Subcommand registration and top-level orchestration |
| `chat.py` | Single-shot chat helper for `butterfly chat` |
| `new_agent.py` | Entity scaffolding (`butterfly entity new`) |

## Available Commands

```bash
butterfly chat MESSAGE                  # new session + send message
butterfly chat --session ID MSG         # send to existing session
butterfly new [ID] [--entity NAME]      # create session (no message)
butterfly sessions                      # list sessions
butterfly log [ID] [-n N] [--watch]     # conversation history
butterfly tasks [ID]                    # session task board
butterfly stop ID                       # stop session
butterfly start ID                      # resume session
butterfly entity new ...                # scaffold a new entity
butterfly server                        # start the server daemon
butterfly web                           # start the web UI

# Auth helpers (v2.0.13+)
butterfly codex login                   # device-code OAuth → ~/.butterfly/auth.json
butterfly codex login --import-codex-cli  # import from ~/.codex/auth.json
butterfly codex login --no-verify       # skip post-login display
butterfly kimi login                    # interactive API key setup → .env
butterfly kimi login --key KEY          # non-interactive (CI-friendly)
butterfly kimi login --no-verify        # skip API ping
butterfly kimi login --env-file PATH    # write to a specific .env file
```

## Server Auto-Start

`butterfly chat` and `butterfly new` automatically start the server daemon if it is not already running. The `_ensure_server_running()` helper checks via `_is_server_running()` and calls `_start_daemon()` if needed, passing through any custom `sessions_dir` and `system_sessions_dir` from CLI args.

## Server Management (butterfly-server)

```bash
butterfly-server                # start (auto-daemonize)
butterfly-server start          # same as above
butterfly-server stop           # graceful shutdown
butterfly-server status         # show running/stopped + PID
butterfly-server update         # reinstall package + restart
butterfly-server --foreground   # run in foreground (no daemonize)
```

All flags work at top level and on subcommands. PID stored in `_sessions/server.pid`, logs in `_sessions/server.log`.
