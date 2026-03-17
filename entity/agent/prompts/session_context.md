---
## Session Files

Your session directory: `sessions/{session_id}/`

- `core/tasks.md` — task board. Read and write via bash.
- `core/memory.md` — persistent memory. Auto-appended to this prompt each activation. Edit via bash.
- `core/skills/` — session-level skills (.md, YAML frontmatter). Loaded each activation. Write/delete via bash.
- `core/tools/` — tool definitions (.json + .sh, loaded each activation). Create to add new tools.
- `core/params.json` — runtime config: `model`, `provider`, `heartbeat_interval`, `tool_providers`.
  - `tool_providers.web_search`: `"brave"` (default) or `"tavily"` — edit to switch search backend.
- `core/system.md`, `core/heartbeat.md` — your prompts (editable).
- `docs/` — user-uploaded files and documents (read-only).
- `playground/` — your free workspace for temp files, scripts, experiments.
- `_sessions/{session_id}/` — system internals (context, events, manifest, status). Do not edit.
