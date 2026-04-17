# CLI — Implementation

## Files

| File | Purpose |
|------|---------|
| `main.py` | Subcommand registration and top-level orchestration |
| `chat.py` | Single-shot chat helper for `butterfly chat` |
| `new_agent.py` | Agent scaffolding (`butterfly agent new`) |

## Available Commands

```bash
butterfly                               # boot server + web UI; print URL; hang
butterfly chat MESSAGE                  # new session + send message
butterfly chat --session ID MSG         # send to existing session
butterfly new [ID] [--agent NAME]       # create session (no message)
butterfly sessions                      # list sessions
butterfly log [ID] [-n N] [--watch]     # conversation history
butterfly tasks [ID]                    # session task board
butterfly stop ID                       # stop session
butterfly start ID                      # resume session
butterfly agent new ...                 # scaffold a new agent
butterfly server                        # tail the running server's log (read-only)
butterfly update [--skip-frontend]      # git pull + pip install + rebuild web + restart

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

## Server Lifecycle (unified CLI, v2.0.16)

The separate `butterfly` + `-server` / `-web` console scripts were removed in v2.0.16. Everything flows through the single `butterfly` entry point:

| Command | Behavior |
|---------|----------|
| `butterfly` | Backgrounds the server daemon (`_start_daemon`) if none is running, then runs uvicorn in-process; prints `http://localhost:7720`; blocks until Ctrl+C. Ctrl+C stops the server **only if this invocation started it**; a pre-existing daemon is left alone so another `butterfly` session keeps working. |
| `butterfly server` | Tails `_sessions/server.log` via `tail -F`. Read-only. Exits with "not running" if the daemon is down. |
| `butterfly update` | `git status --porcelain` refuses on dirty/untracked; stops the server **if it was running**; `git pull --ff-only` + `pip install -e .` + `npm run build` (unless `--skip-frontend`); restarts the server if it was running. Restores the server on git / pip failure so the user is never left without a daemon. If no server was running at start, none is started at end. |

Invoke the server module directly with `python -m butterfly.runtime.server --foreground` for in-process daemon use (this is what `_start_daemon` Popens and what the auto-update `execvp` path rebinds to). PID stored in `_sessions/server.pid`, logs in `_sessions/server.log`.
