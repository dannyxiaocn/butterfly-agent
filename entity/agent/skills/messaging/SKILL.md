---
name: messaging
description: >
  Send and receive messages between agent sessions using nutshell's built-in
  communication tools. Use when you need to talk to another agent, check who
  is online, coordinate work across sessions, or the user asks about
  multi-agent communication, friends, or messaging.
---

## Overview

Nutshell agents can communicate in real time using two mechanisms:

| Mechanism | Purpose |
|-----------|---------|
| `nutshell friends` (CLI / bash) | See who is online — like an IM contact list |
| `send_to_session` (built-in tool) | Send a message to another session |

**You already have `send_to_session` as a tool.** This skill teaches you
*when* and *how* to use it effectively for multi-agent communication.

---

## Step 1 — Discover Peers

Before messaging, find out who is available:

```bash
nutshell friends              # human-readable table
nutshell friends --json       # machine-readable (for parsing)
```

Output example:
```
● agent           (2026-03-25_10-00-00)  online   last: 2m ago
◐ nutshell_dev    (2026-03-25_09-00-00)  idle     last: 35m ago
○ agent           (2026-03-24_08-00-00)  offline  last: 1d ago
```

Status indicators:
- **● online** — actively running or responded within 5 minutes
- **◐ idle** — responded within the last hour
- **○ offline** — no activity for >1 hour, or explicitly stopped

### Parsing JSON output

```bash
# Get online session IDs
nutshell friends --json | python3 -c "
import json, sys
friends = json.load(sys.stdin)
online = [f for f in friends if f['status'] == 'online']
for f in online:
    print(f['id'], f['entity'])
"
```

---

## Step 2 — Send a Message

Use `send_to_session` to talk to a peer:

```
send_to_session(
    session_id="2026-03-25_10-00-00",
    message="Hey, can you summarise the test results in playground/output/results.json?",
    mode="sync",       # wait for reply
    timeout=120,       # seconds to wait
)
```

### Sync vs Async

| Mode | Behaviour | Use when |
|------|-----------|----------|
| `sync` | Block until the other agent replies | You need the answer now |
| `async` | Fire-and-forget, return immediately | Kick off background work |

**Default is `sync` with 60s timeout.** For heavy tasks, increase timeout.

---

## Step 3 — Communication Patterns

### Ask a question and get an answer

```
reply = send_to_session(
    session_id="<peer-id>",
    message="What is the status of the data pipeline?",
    mode="sync",
    timeout=120,
)
# reply contains the other agent's response text
```

### Delegate a task

```
send_to_session(
    session_id="<peer-id>",
    message="Please run pytest tests/ -q and report the results back.",
    mode="sync",
    timeout=180,
)
```

### Broadcast to multiple agents

```bash
# First, get all online peers
nutshell friends --json > /tmp/friends.json
```

Then iterate and send:

```
# In your tool calls — send to each online peer sequentially
send_to_session(session_id="<id-1>", message="Team standup: what are you working on?", mode="sync", timeout=60)
send_to_session(session_id="<id-2>", message="Team standup: what are you working on?", mode="sync", timeout=60)
```

### Spawn + message (new agent)

```
result = spawn_session(
    entity="agent",
    initial_message="You are a research assistant. Wait for questions.",
)
# result["session_id"] is the new peer's ID
# Now message it:
send_to_session(
    session_id=result["session_id"],
    message="Research the latest advances in transformer architectures.",
    mode="sync",
    timeout=300,
)
```

---

## Rules & Gotchas

1. **Never message yourself** — `send_to_session` blocks this, but avoid trying.
2. **No circular calls** — A→B→A deadlocks. Design communication as a DAG.
3. **Offline agents won't reply** — check `nutshell friends` first. If the peer is offline, use `nutshell start <id>` to wake them, or spawn a new session.
4. **Persist peer IDs** — write important session IDs to `core/memory.md` so you remember them across activations.
5. **Timeouts** — if a peer is doing heavy work, increase timeout. Default 60s is fine for quick questions.
6. **The server must be running** — `send_to_session` and `spawn_session` require `nutshell-server` to be active.

---

## Quick Reference

```bash
# Who is online?
nutshell friends

# Send a message (in your tool calls)
send_to_session(session_id="<ID>", message="...", mode="sync", timeout=120)

# Spawn a new peer
spawn_session(entity="agent", initial_message="...")

# Wake a stopped peer
nutshell start <SESSION_ID>
```
