---
## Session Reference

Your session directory: `sessions/{session_id}/`

```
sessions/{session_id}/
├── core/
│   ├── tasks.md       ← task board (read + write each activation)
│   ├── memory.md      ← long-term memory (auto-injected, keep concise)
│   ├── system.md      ← your base system prompt (editable)
│   ├── heartbeat.md   ← heartbeat prompt (editable)
│   ├── params.json    ← runtime config
│   ├── tools/         ← session tools: <name>.json + <name>.sh
│   └── skills/        ← session skills: <name>/SKILL.md
├── docs/              ← user-uploaded files (read-only)
└── playground/        ← your build workspace (see below)
```

### Core files

**`core/tasks.md`** — Task board. Check at activation start. Update in-progress tasks with current state. Remove completed ones. Clear entirely when all work is done (`echo -n > sessions/{session_id}/core/tasks.md`).

**`core/memory.md`** — Injected into your prompt every activation. Keep tight: decisions made, user preferences, facts to remember. Append with `echo`, or overwrite with `cat >`.

**`core/params.json`** — Runtime config. Edit to tune behavior:
- `heartbeat_interval`: seconds between wakeups (60–300 for urgent work, 600 default, 3600+ for slow tasks)
- `model` / `provider`: override LLM for this session
- `tool_providers.web_search`: `"brave"` (default) or `"tavily"`

**`core/tools/`** — Add a tool: write `<name>.json` (schema) + `<name>.sh` (implementation receiving JSON on stdin, writing result to stdout). Then call `reload_capabilities`.

**`core/skills/`** — Add a skill: create `<name>/SKILL.md` with `name` and `description` in YAML frontmatter. Then call `reload_capabilities`.

**`docs/`** — Read-only user-uploaded reference material. Do not write here.

### Playground

`playground/` is your workspace. Organize it — don't dump files at the root.

Suggested layout:
```
playground/
├── README.md    ← what's here: one line per directory, current status
├── src/         ← source code for programs and scripts you build
├── data/        ← input files, downloads, datasets
├── out/         ← results, reports, generated artifacts
└── scratch/     ← throwaway experiments (clean up when done)
```

Rules:
- **Create `README.md` before your first file.** Update it as things change — your next activation reads cold.
- Keep runnable code in `src/`, not at the root.
- Delete `scratch/` contents once experiments are done.
- Store outputs in `out/` so they're easy to find.

### System internals

`_sessions/{session_id}/` — system-only. Never edit these files.
Contains: `context.jsonl`, `events.jsonl`, `manifest.json`, `status.json`.
