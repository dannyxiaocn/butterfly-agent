# CLI — Implementation

## Files

| File | Purpose |
|------|---------|
| `main.py` | Subcommand registration and top-level orchestration |
| `chat.py` | Legacy single-shot chat helper for `nutshell chat` |
| `new_agent.py` | Entity scaffolding |
| `friends.py`, `kanban.py`, `visit.py` | Read-only session views |
| `repo_skill.py` | Repo overview skill generation |

## Common Commands

```bash
nutshell chat "message"
nutshell new --entity agent
nutshell sessions
nutshell log <id>
nutshell prompt-stats <id>
nutshell token-report <id>
nutshell meta <entity>
```

## Server Auto-Start

`nutshell chat` and `nutshell new` automatically start the server daemon if it is not already running. The `_ensure_server_running()` helper checks via `_is_server_running()` and calls `_start_daemon()` if needed, passing through any custom `sessions_dir` and `system_sessions_dir` from CLI args.

## Server Management (nutshell-server)

```bash
nutshell-server                # start (auto-daemonize)
nutshell-server start          # same as above
nutshell-server stop           # graceful shutdown
nutshell-server status         # show running/stopped + PID
nutshell-server update         # reinstall package + restart
nutshell-server --foreground   # run in foreground (no daemonize)
```

All flags work at top level and on subcommands. PID stored in `_sessions/server.pid`, logs in `_sessions/server.log`.
