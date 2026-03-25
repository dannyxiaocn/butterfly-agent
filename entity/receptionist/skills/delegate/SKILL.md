---
name: delegate
description: >
  Delegate tasks to a worker (core) agent using spawn_session + send_to_session.
  Covers the full lifecycle: spawning, task dispatch, progress monitoring, result
  collection, and error handling. Use whenever the receptionist needs to hand off
  real work to a background agent.
---

## Delegation Lifecycle

### Step 1 — Spawn a worker

```python
spawn_session(
    entity="agent",                    # or a specialised entity
    initial_message="<task description with full context>",
    heartbeat=120,                     # lower = more responsive
)
# → {"session_id": "2026-XX-XX_HH-MM-SS"}
```

**Always save the session ID** to `core/memory.md`:
```bash
echo "## Active Workers" >> core/memory.md
echo "- task_name: SESSION_ID (entity: agent, started: TIMESTAMP)" >> core/memory.md
```

### Step 2 — Monitor progress

For quick tasks (< 2 min), use sync mode and wait:
```python
send_to_session(
    session_id="SESSION_ID",
    message="Have you finished? Please give me the final result.",
    mode="sync",
    timeout=180,
)
```

For longer tasks, use async check-ins:
```python
# Fire a status check
send_to_session(
    session_id="SESSION_ID",
    message="What's your current progress?",
    mode="sync",
    timeout=60,
)
```

### Step 3 — Collect and relay results

When the worker replies with results:
1. **Summarize** for the user — don't dump raw output
2. **Reference artifacts** — if the worker wrote files, tell the user where
3. **Clean up memory** — mark the worker as done in `core/memory.md`

### Step 4 — Handle failures

If `send_to_session` times out or the worker reports an error:
1. **Check worker health** via bash:
   ```bash
   cat _sessions/SESSION_ID/status.json 2>/dev/null
   cat _sessions/SESSION_ID/core/tasks.md 2>/dev/null
   ```
2. **Retry** by sending a follow-up message with clarifications
3. **Escalate** to the user if the worker is stuck: "The task hit an issue — here's what happened: ..."

---

## Choosing the Right Entity

| Entity | When to use |
|--------|-------------|
| `agent` | General tasks — coding, research, writing, file processing |
| `kimi_agent` | Long-context tasks — large document analysis (128k context) |
| `nutshell_dev` | Nutshell project development tasks specifically |

List all available entities: `ls entity/`

---

## Task Description Best Practices

When writing the `initial_message` for a worker, include:

1. **Goal** — what the worker should accomplish
2. **Context** — background info, file locations, constraints
3. **Output** — where to write results and in what format
4. **Success criteria** — how to know the task is done

**Example:**
```
Research the top 5 Python web frameworks released in 2025.
Write a comparison table to playground/output/frameworks.md.
Include: name, stars, key feature, learning curve rating (1-5).
When done, reply with a one-paragraph summary.
```

---

## Parallel Delegation

For tasks that naturally decompose:

```python
workers = []
for subtask in subtasks:
    result = spawn_session(
        entity="agent",
        initial_message=f"Subtask: {subtask}",
        heartbeat=60,
    )
    workers.append(result["session_id"])

# Collect results from all workers
for sid in workers:
    reply = send_to_session(session_id=sid, message="Result?", mode="sync", timeout=120)
```

---

## Anti-Patterns

- ❌ **Don't delegate trivial tasks** — answering "what time is it?" doesn't need a worker
- ❌ **Don't forget session IDs** — always persist them to memory
- ❌ **Don't instruct workers to call you back** — causes deadlocks (A→B→A)
- ❌ **Don't spawn workers without monitoring** — always follow up
- ❌ **Don't relay raw worker output** — summarize for the user
