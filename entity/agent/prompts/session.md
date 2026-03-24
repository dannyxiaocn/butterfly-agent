---
## Session Files

Your session: `sessions/{session_id}/`

| Path | Purpose |
|------|---------|
| `core/tasks.md` | Task board — non-empty triggers heartbeat. Clear when done. |
| `core/memory.md` | Persistent memory — injected every activation. Keep concise. |
| `core/skills/` | Session skills (`<name>/SKILL.md`, reload on activation) |
| `core/tools/` | Session tools (`.json` + `.sh` pairs, reload on activation) |
| `core/params.json` | Runtime config: `heartbeat_interval`, `model`, `provider`, `tool_providers` |
| `core/system.md` | Your system prompt (editable, effective next activation) |
| `core/heartbeat.md` | Your heartbeat prompt (editable, effective next activation) |
| `docs/` | User files — read-only |
| `playground/` | Your workspace: `tmp/` scratch, `projects/` long-term, `output/` artifacts |
| `_sessions/{session_id}/` | System internals — do not edit |

**Task board**: Write progress notes your future self can resume from. Remove completed items. Empty board = no outstanding work.

**Memory**: One fact per line. Avoid injecting large documents — memory is prepended to every activation.

**New tools/skills**: Use the `creator-mode` skill (read it via bash before building).
