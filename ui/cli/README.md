# `ui/cli`

The CLI frontend for Nutshell. `main.py` is the primary entrypoint behind the `nutshell` command.

## What This Part Is

- `main.py`: subcommand registration and top-level orchestration.
- `chat.py`: legacy single-shot chat helper used by `nutshell chat`.
- `new_agent.py`: entity scaffolding.
- `friends.py`, `kanban.py`, `visit.py`: read-only session views.
- `repo_skill.py`: repo overview skill generation and repo-dev session helper.

## How To Use It

Common commands:

```bash
nutshell chat "message"
nutshell new --entity agent
nutshell sessions
nutshell log <id>
nutshell prompt-stats <id>
nutshell token-report <id>
nutshell meta <entity>
```

## How It Contributes To The Whole System

This directory is the operator surface for session lifecycle, diagnostics, and entity management. It uses the same session files and runtime bridge as the Web UI.

