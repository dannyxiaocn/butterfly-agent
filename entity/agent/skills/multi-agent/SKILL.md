---
name: multi-agent
description: >
  Coordinate multi-agent workflows: spawn sub-agents, delegate subtasks, build
  agent pipelines, implement worker/coordinator patterns. Use when a task is too
  large or complex for a single agent, when subtasks can run in parallel, when
  you need a specialised agent for a domain (research, coding, writing), or when
  the user asks for multi-agent, parallel agents, or sub-agent workflows.
---

## Overview

You have two built-in tools for multi-agent coordination:

| Tool | When to use |
|------|-------------|
| `spawn_session` | Create a new agent session from an entity |
| `send_to_session` | Send a message to a running session; sync or async |

**nutshell-server must be running** for spawned sessions to activate. If it is not running, sessions are queued and will activate when the server starts.

---

## Core Patterns

### 1 — Delegate and Wait (Sync)

Spawn a sub-agent, give it a task, wait for the reply.

```
spawn_session(
    entity="agent",
    initial_message="Summarise the key findings in playground/data/report.csv",
)
# → returns {"session_id": "2026-03-25_10-00-00"}

send_to_session(
    session_id="2026-03-25_10-00-00",
    message="Are you done? Please give me your final summary.",
    mode="sync",
    timeout=120,
)
```

Use for: sequential pipelines, reviewer agents, gated workflows.

### 2 — Fire-and-Forget (Async)

Kick off a long job and continue your own work.

```
spawn_session(
    entity="agent",
    initial_message="Run the full data pipeline in playground/pipeline.py and write results to playground/output/results.json",
    heartbeat=60,
)
# Don't call send_to_session yet — the sub-agent is working.
# Poll later with send_to_session(mode="sync") when you need the result.
```

Use for: background jobs, parallel work streams, overnight tasks.

### 3 — Worker Pool

Spawn N specialised workers, collect results.

```
# Spawn workers
workers = []
for topic in ["climate", "energy", "water"]:
    r = spawn_session(
        entity="agent",
        initial_message=f"Research '{topic}' and write a 200-word brief to playground/output/{topic}.md",
        heartbeat=60,
    )
    workers.append(r["session_id"])

# After workers finish, collect results
summaries = []
for session_id in workers:
    reply = send_to_session(
        session_id=session_id,
        message="Did you finish? Paste your brief here.",
        mode="sync",
        timeout=180,
    )
    summaries.append(reply)
```

### 4 — Coordinator / Receptionist Split

Run a dedicated coordination agent. You stay as the "core" agent; spawn a "receptionist" that handles inbound messages or status queries.

```
spawn_session(
    entity="agent",
    initial_message=(
        "You are the receptionist for project X.\n"
        "Maintain a status board in playground/status.md.\n"
        "When asked about project status, read status.md and reply.\n"
        "When given updates, write them to status.md."
    ),
    heartbeat=300,
)
```

---

## Communication Protocol

**Avoid deadlocks.** A → B → A will deadlock. Design your call graph as a DAG.

**Always pass a `timeout`.** The default (60 s) may be too short for heavy tasks. Estimate the task time and add a buffer.

**Persist session IDs.** Write spawned session IDs to `core/memory.md` or `playground/sessions.json` so you can reference them across activations.

```bash
# Save session IDs to memory
echo "- sub-agent pipeline: 2026-03-25_10-00-00" >> sessions/YOUR_ID/core/memory.md
```

**Check sub-agent health.** If `send_to_session` times out, the sub-agent may have crashed or the server may be down. Check `_sessions/<id>/status.json` via bash.

```bash
cat _sessions/2026-03-25_10-00-00/status.json
```

---

## Choosing an Entity

| Entity | Best for |
|--------|----------|
| `agent` | General-purpose tasks, coding, research, writing |
| `kimi_agent` | Long-context tasks (Kimi's 128k context), document analysis |

To see all available entities: `ls entity/`

---

## Gotchas

- `send_to_session` **cannot message yourself** (NUTSHELL_SESSION_ID guard).
- Spawned sessions require **nutshell-server** running to activate their heartbeat.
- Sub-agent output lands in its own `context.jsonl` — you read it via `send_to_session`, not by reading files directly.
- If a sub-agent needs to write shared output, use a **shared path** in playground: `sessions/YOUR_ID/playground/shared/`.
- `heartbeat` controls how often the sub-agent wakes autonomously (60 s = urgent, 600 s = normal). Set it low for time-sensitive jobs.
