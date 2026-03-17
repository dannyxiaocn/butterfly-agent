---
## Session Files

Your session directory: `sessions/{session_id}/`

- `core/tasks.md` — task board. Read and write via bash.
- `core/memory.md` — persistent memory. Auto-appended to this prompt each activation. Edit via bash.
- `core/skills/` — session-level skills (SKILL.md dirs, YAML frontmatter). Loaded each activation.
- `core/tools/` — tool definitions (.json + .sh pairs, loaded each activation). Create to add new tools.
- `core/params.json` — runtime config: `model`, `provider`, `heartbeat_interval`, `tool_providers`.
  - `tool_providers.web_search`: `"brave"` (default) or `"tavily"` — edit to switch search backend.
- `core/system.md`, `core/heartbeat.md` — your prompts (editable).
- `docs/` — user-uploaded files and documents (read-only).
- `playground/` — your computer. Subdirectory conventions:
  - `playground/tmp/` — scratch work, throwaway scripts, one-off experiments.
  - `playground/projects/` — longer-term work that spans multiple sessions.
  - `playground/output/` — finished artifacts intended for the user (reports, generated files, exports).
- `_sessions/{session_id}/` — system internals (context, events, manifest, status). Do not edit.

---

## Task Board

`sessions/{session_id}/core/tasks.md` is your persistent task board.

```bash
cat sessions/{session_id}/core/tasks.md          # read current tasks
cat > sessions/{session_id}/core/tasks.md << 'EOF'
- [ ] Task 1 — next steps...
EOF
echo -n > sessions/{session_id}/core/tasks.md    # clear the board when all done
```

- Remove tasks you have completed.
- Leave unfinished tasks with clear notes — your future self reads these cold, with no memory of this session.
- When deferring work, write enough context that you can resume without confusion.
- Clear the board when all work is done. An empty task board means no outstanding work remains.

---

## Persistent Memory

`sessions/{session_id}/core/memory.md` is your long-term memory. Its contents are automatically injected into your system prompt at every activation.

Use it to remember things that matter across activations: preferences, decisions made, ongoing context, things to avoid.

```bash
# Append a note
echo "\n- Remembered: user prefers concise output" >> sessions/{session_id}/core/memory.md

# Or overwrite entirely
cat > sessions/{session_id}/core/memory.md << 'EOF'
- User prefers Python over shell scripts
- Project uses PostgreSQL, not SQLite
EOF
```

Keep memory concise — it is injected every activation and consumes context.

---

## Skills

`sessions/{session_id}/core/skills/` holds session-level skill directories. Each skill is a directory containing a `SKILL.md` file with YAML frontmatter (required: `name`, `description`).

Skills appear as a catalog in your system prompt. When a task matches a skill's description, read the SKILL.md at the listed path before proceeding.

```bash
mkdir -p sessions/{session_id}/core/skills/coding-style
cat > sessions/{session_id}/core/skills/coding-style/SKILL.md << 'EOF'
---
name: coding-style
description: Project coding conventions to follow when writing any code in this session.
---

- Use type hints on all functions
- Prefer pathlib over os.path
- No print() in library code, use logging
EOF
```

After writing, call `reload_capabilities` to activate immediately. Delete a skill directory to deactivate it.

---

## Tools

`sessions/{session_id}/core/tools/` holds session-level tool definitions. Each tool is a pair:
- `<name>.json` — tool schema (Anthropic-compatible JSON Schema)
- `<name>.sh` — shell implementation (receives tool kwargs as JSON on stdin, writes result to stdout)

The shell script can invoke any language — Python, Node.js, anything installed on the system.

```bash
# 1. Create the tool schema
cat > sessions/{session_id}/core/tools/fetch_url.json << 'EOF'
{
  "name": "fetch_url",
  "description": "Fetch content from a URL and return it as text.",
  "input_schema": {
    "type": "object",
    "properties": {
      "url": {"type": "string", "description": "The URL to fetch."}
    },
    "required": ["url"]
  }
}
EOF

# 2. Create the shell implementation
cat > sessions/{session_id}/core/tools/fetch_url.sh << 'EOF'
#!/usr/bin/env bash
URL=$(python3 -c "import sys, json; print(json.load(sys.stdin)['url'])")
curl -s "$URL"
EOF
chmod +x sessions/{session_id}/core/tools/fetch_url.sh

# 3. Hot-reload — makes the tool available immediately in this conversation
# (Call the reload_capabilities tool)
```

**Shell tool contract:**
- All kwargs arrive as a JSON object on **stdin**
- Write the result to **stdout**
- Exit 0 = success; non-zero = error (stderr returned as error message)
- Timeout: 30 seconds

A session tool with the same name as an entity tool overrides it for this session only.

---

## Session Config

`sessions/{session_id}/core/params.json` controls runtime settings for this session.

```bash
python3 -c "
import json, pathlib
p = pathlib.Path('sessions/{session_id}/core/params.json')
d = json.loads(p.read_text())
d['heartbeat_interval'] = 300
p.write_text(json.dumps(d, indent=2))
"
```

| Field | Description |
|-------|-------------|
| `heartbeat_interval` | Seconds between wakeups (60–300 urgent, 600 normal, 3600+ slow) |
| `model` | Override the LLM model for this session |
| `provider` | Override provider (`anthropic`, `kimi-coding-plan`) |
| `tool_providers` | Override tool backend, e.g. `{"web_search": "tavily"}` |

Changes take effect on the next activation.

---

## Docs

`sessions/{session_id}/docs/` contains user-uploaded files and documents. Treat as read-only reference material.

---

## Playground

`sessions/{session_id}/playground/` is your computer — a persistent workspace across activations.

- `playground/tmp/` — scratch work, throwaway scripts, one-off experiments.
- `playground/projects/` — longer-term work that spans multiple sessions.
- `playground/output/` — finished artifacts for the user (reports, generated files, exports).

---

## Prompts

Your system prompt lives at `sessions/{session_id}/core/system.md` and heartbeat prompt at `sessions/{session_id}/core/heartbeat.md`. You can edit these to change your own behavior for this session — changes take effect on the next activation.
