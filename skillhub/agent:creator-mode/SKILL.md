---
name: agent:creator-mode
description: >
  Create or modify tools (.json + .sh pairs) and skills (SKILL.md files) and
  hot-reload them into the active conversation using reload_capabilities. Use when
  you want to build a new capability, extend yourself with a new tool, fix an existing
  tool, create or update a skill, or build a complete application (web server, API,
  data pipeline, CLI) — all without restarting.
---

## Overview

You can extend yourself at runtime. New tools and skills take effect immediately via `reload_capabilities` — no session restart required.

Session-scoped capabilities live in `core/` inside your session directory:

- **Tools**: `core/tools/<name>.json` (schema) + `core/tools/<name>.sh` (implementation)
- **Skills**: `core/skills/<name>/SKILL.md` (frontmatter + body)

**There is no limit on what a tool can do.** The shell script can call Python, Node.js, any language or binary on the system. Build first, use immediately.

---

## Building a tool

Tools in the system live in two places:

- **Shared tools (toolhub)**: `toolhub/<name>/` with `tool.json` (schema) + `executor.py` (implementation). These are available to all sessions and managed centrally.
- **Session-local tools**: `core/tools/<name>.json` (schema) + `core/tools/<name>.sh` (implementation). Agent-created tools use this pattern — they live in your session and take effect after `reload_capabilities`.

### Creating a session-local tool

1. Write the JSON schema to `core/tools/<name>.json`
2. Write the shell implementation to `core/tools/<name>.sh`
3. `chmod +x core/tools/<name>.sh`
4. Call `reload_capabilities`
5. Test by calling the tool

### JSON schema template

```json
{
  "name": "my_tool",
  "description": "What this tool does. Be specific — the model reads this to decide when to call it.",
  "input_schema": {
    "type": "object",
    "properties": {
      "arg1": {"type": "string", "description": "Description of arg1."}
    },
    "required": ["arg1"]
  }
}
```

### Shell tool contract

- All kwargs arrive as a JSON object on **stdin**
- Write result to **stdout**
- Exit 0 = success; non-zero = error (stderr returned as error message)
- Timeout: 30 seconds

```bash
#!/usr/bin/env bash
python3 << 'PYEOF'
import sys, json
args = json.load(sys.stdin)
result = args['arg1'].upper()
print(result)
PYEOF
```

### Building full applications

Since `.sh` can do anything, tools can build and drive complete applications:

**Persistent background process (e.g., web server)**
```bash
#!/usr/bin/env bash
PORT=$(python3 -c "import sys,json; print(json.load(sys.stdin).get('port',8000))")
nohup python3 sessions/YOUR_ID/playground/app.py --port "$PORT" \
  > sessions/YOUR_ID/playground/server.log 2>&1 &
echo "Server started on port $PORT (PID $!)"
```

**Script runner**
```bash
#!/usr/bin/env bash
SCRIPT=$(python3 -c "import sys,json; print(json.load(sys.stdin)['path'])")
python3 "$SCRIPT"
```

**Data pipeline step**
```bash
#!/usr/bin/env bash
python3 << 'PYEOF'
import sys, json, csv
args = json.load(sys.stdin)
with open(args['path']) as f:
    rows = list(csv.DictReader(f))
print(json.dumps({"rows": len(rows), "columns": list(rows[0].keys())}))
PYEOF
```

---

## Creating a skill

A skill is a reusable block of instructions or domain knowledge injected into your system prompt. Once created and reloaded, it appears in your `<available_skills>` catalog.

**When to create a skill:**
- Recurring workflow the user wants repeated consistently
- Domain knowledge specific to this session or project
- A multi-step process the user wants captured as a repeatable procedure

### Anatomy of a SKILL.md

```markdown
---
name: skill-name      # identifier, matches directory name
description: >        # THE TRIGGER — when and why to use this skill.
  Be specific. Include task types, user phrases, and contexts.
  More detail = more reliable triggering.
---

Instructions in Markdown. Write in imperative form.
Explain the *why* behind each instruction.
```

**The `description` field is the triggering mechanism.** You decide whether to read a skill based solely on name + description. Make it answer: "When exactly should I use this?"

### Creating a skill

```bash
mkdir -p sessions/YOUR_ID/core/skills/my-skill
cat > sessions/YOUR_ID/core/skills/my-skill/SKILL.md << 'EOF'
---
name: my-skill
description: >
  Describe when to trigger this skill. List task types, user phrases,
  and contexts where it applies.
---

## Instructions

Write instructions here.
EOF
# Call reload_capabilities
```

### Deactivating a skill

```bash
rm -rf sessions/YOUR_ID/core/skills/my-skill
# Call reload_capabilities
```

---

## Task board

`core/tasks/` drives task execution. Each `.json` file is a task card. Use the `manage_task` tool to create, update, and manage tasks.

### Task card JSON format

```json
{
  "name": "my-task",
  "description": "What this task does",
  "status": "pending",
  "interval": 3600,
  "created_at": "2026-04-12T10:00:00",
  "last_started_at": null,
  "last_finished_at": null,
  "comments": "",
  "progress": ""
}
```

### Status values

- **pending** — task is waiting for next trigger (default state for new & recurring tasks)
- **working** — task is currently being executed
- **finished** — task completed (one-shot) or manually finished
- **paused** — user-initiated pause; won't fire until user explicitly resumes

### Managing tasks

```bash
# List task cards
ls sessions/YOUR_ID/core/tasks/*.json

# Read a task
cat sessions/YOUR_ID/core/tasks/duty.json
```

Use the `manage_task` tool to create/update/delete tasks programmatically. **Write progress notes your future self can resume from cold** — use the `progress` and `comments` fields to track state across activations.

---

## Persistent memory

`core/memory.md` is injected into your system prompt every activation.

```bash
# Append a fact
echo "- User prefers Python over shell scripts" >> sessions/YOUR_ID/core/memory.md

# Overwrite entirely
cat > sessions/YOUR_ID/core/memory.md << 'EOF'
- Project uses PostgreSQL
- User prefers concise output
EOF
```

Keep memory concise — it consumes context every activation. One fact per line. Avoid pasting large documents.

---

## Runtime config (config.yaml)

`core/config.yaml` controls session runtime — model, provider, thinking settings, tool/skill lists. Changes take effect on the next activation.

```bash
cat sessions/YOUR_ID/core/config.yaml
# Edit with any text editor or bash
```

Key fields: `model`, `provider`, `thinking`, `thinking_budget`, `tool_providers`, `duty` (recurring task config).

---

## The iteration loop

```
build/edit → reload_capabilities → test → fix → reload_capabilities → …
```

Each reload replaces the previous version in memory. No penalty for iteration.

**Testing a tool:**
```bash
# Directly invoke the .sh to confirm it works before the LLM calls it
echo '{"arg1": "value"}' | bash sessions/YOUR_ID/core/tools/my_tool.sh
```

**Testing a skill:** Create a scratch note, call `reload_capabilities`, then explicitly invoke the skill in a follow-up message. If the skill is not triggering, make the `description` field longer and more specific — include the exact phrases/contexts where it applies.

---

## Making improvements stick

Session tools/skills are local to this session. If you want an improvement to persist for future sessions, edit the checked-out repository copy directly with `bash`, update the relevant entity files, run tests, and commit the change in that repo.

---

## Built-in tools reference

System tools loaded by default (always available, no .json needed in core/tools/):

| Tool | Purpose |
|------|---------|
| `bash` | Execute shell commands |
| `web_search` | Search the web via Brave/Tavily |
| `reload_capabilities` | Hot-reload tools + skills from core/ |

---

## Gotchas

- `reload_capabilities` cannot be overridden — always injected last; any disk tool with that name is filtered out.
- A session tool with the same name as an entity tool overrides it — use this to patch a built-in for this session.
- Background processes are not managed by the session — store the PID in the playground if you need to stop them.
- **Skill descriptions must be specific.** Vague descriptions cause under-triggering. Include exact user phrases and task types.
