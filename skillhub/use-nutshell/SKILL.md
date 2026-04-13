---
name: use-nutshell
description: >
  Nutshell CLI usage guide. Load this skill when the user asks how to use
  nutshell, wants to run CLI commands, manage sessions, create entities,
  view logs, or interact with the nutshell agent system from the terminal.
---

# Nutshell CLI Guide

Nutshell is a minimal Python agent runtime. Agents persist across conversations via filesystem-based sessions.

## Core Concepts

- **Entity** — a reusable agent template (`entity/<name>/`): config, prompts, tools, skills
- **Session** — a running instance of an entity (`sessions/<id>/`): agent-visible workspace
- **Meta session** — mutable shared seed for all sessions of an entity (`sessions/<entity>_meta/`)
- **Server** — background daemon that watches for sessions and runs agent loops

## Quick Start

```bash
# Start the server (auto-daemonizes)
nutshell server
# or directly:
nutshell-server start

# Send a message (auto-starts server if needed)
nutshell chat "Hello, what can you do?"

# Use a specific entity
nutshell chat --entity nutshell_dev "Review the codebase"

# Continue an existing session
nutshell chat --session 2026-04-13_10-00-00-a1b2 "What's the status?"
```

## All CLI Commands

### Session Interaction

| Command | Description |
|---------|-------------|
| `nutshell chat MESSAGE` | Send a message; creates a new session or continues one |
| `nutshell new [ID]` | Create a session without sending a message |
| `nutshell stop SESSION_ID` | Stop a session's heartbeat loop |
| `nutshell start SESSION_ID` | Resume a stopped session |

**`chat` flags:**
- `--session ID` — continue an existing session
- `--entity NAME` — entity to use (default: `agent`)
- `--no-wait` — fire-and-forget (don't block for reply)
- `--timeout N` — seconds to wait (default: 300)
- `--keep-alive` — keep server running after reply
- `--inject-memory KEY=VALUE` or `KEY=@FILE` — inject memory layers

**`new` flags:**
- `--entity NAME` — entity to init from (default: `agent`)
- `--heartbeat N` — heartbeat interval in seconds
- `--inject-memory KEY=VALUE` — inject memory at creation

### Monitoring & Views

| Command | Description |
|---------|-------------|
| `nutshell sessions` | List all sessions with status |
| `nutshell log [SESSION_ID]` | Show conversation history |
| `nutshell tasks [SESSION_ID]` | Show task cards |
| `nutshell visit [SESSION_ID]` | Detailed agent room view |
| `nutshell friends` | IM-style contact list (online/idle/offline) |
| `nutshell kanban` | Unified task board across sessions |
| `nutshell prompt-stats [SESSION_ID]` | System prompt size breakdown |
| `nutshell token-report [SESSION_ID]` | Per-turn token usage table |

**`log` flags:**
- `-n N` — number of turns (default: 5)
- `--since TIMESTAMP` — filter by time (ISO-8601, epoch, or `now`)
- `--watch` — poll for new turns every 2s

### Entity Management

| Command | Description |
|---------|-------------|
| `nutshell entity new` | Scaffold a new entity (interactive) |
| `nutshell meta [ENTITY]` | Show entity meta-session info |
| `nutshell dream ENTITY` | Trigger meta session dream cycle |

**`entity new` flags:**
- `-n NAME` — skip interactive prompt
- `--init-from SOURCE` — copy from existing entity
- `--blank` — empty entity with placeholders

### Dev Tools

| Command | Description |
|---------|-------------|
| `nutshell repo-skill REPO_PATH` | Generate a SKILL.md codebase overview |
| `nutshell repo-dev REPO_PATH` | Create a dev session with codebase skill |

**`repo-dev` flags:**
- `-n NAME` — custom skill name
- `-m MSG` — initial message to send

### Server Management

| Command | Description |
|---------|-------------|
| `nutshell server` | Start the server daemon |
| `nutshell-server start` | Start server (auto-daemonizes) |
| `nutshell-server stop` | Stop the server |
| `nutshell-server status` | Check if server is running |
| `nutshell-server update` | Stop, reinstall, restart |
| `nutshell-server --foreground` | Run in current process |

## Session Lifecycle

```
entity/ ──create──> sessions/<id>/ ──chat──> agent runs ──stop──> napping
                                                  ↑                  │
                                                  └────start─────────┘
```

1. `nutshell new` or `nutshell chat` creates a session from an entity template
2. Server picks up the session and runs the agent loop
3. Agent reads/writes `core/` files (memory, tasks, tools, skills)
4. `nutshell stop` pauses; `nutshell start` resumes

## Practical Workflows

**Quick one-shot question:**
```bash
nutshell chat "Explain how Python generators work"
```

**Long-running dev session:**
```bash
nutshell new --entity nutshell_dev my-feature
nutshell chat --session my-feature "Add pagination to the API"
nutshell log --session my-feature --watch  # monitor progress
```

**Inject context into a session:**
```bash
nutshell chat --inject-memory spec=@design_doc.md "Implement this spec"
```

**Work on an external codebase:**
```bash
nutshell repo-dev ~/projects/my-app -m "Add unit tests for the parser"
```

**Check all agent activity:**
```bash
nutshell friends     # who's online?
nutshell kanban      # what's everyone working on?
```
