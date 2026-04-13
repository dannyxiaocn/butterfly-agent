---
## Session Files

Your session: `sessions/{session_id}/`

| Path | Purpose |
|------|---------|
| `core/tasks/` | Task cards (`<name>.json`) with scheduling and status management. `default.json` is the recurring default task card. |
| `core/memory.md` | Persistent memory — injected every activation. Keep concise. |
| `core/apps/` | App notifications (`<app>.md` files, injected into system prompt each activation) |
| `core/skills/` | Session skills (`<name>/SKILL.md`, reload on activation) |
| `core/tools/` | Session tools (`.json` + `.sh` pairs, reload on activation) |
| `core/config.yaml` | Runtime config: `model`, `provider`, `tool_providers` |
| `core/system.md` | Your system prompt (editable, effective next activation) |
| `core/task.md` | Your task prompt (editable, effective next activation) |
| `docs/` | User files — read-only |
| `playground/` | Your workspace: `tmp/` scratch, `projects/` long-term, `output/` artifacts |
| `_sessions/{session_id}/` | System internals — do not edit |

**bash default directory**: `sessions/{session_id}/` — use short relative paths: `ls core/tasks`, `cat core/tasks/default.json`, `ls playground/`. Use `workdir=...` to override per call.

**Task cards**: Each task lives in `core/tasks/<name>.json`. Update the relevant card with progress notes your future self can resume from. Mark completed work by updating that card's status or content instead of maintaining a separate flat board file.

**Memory**: One fact per line. Avoid injecting large documents — memory is prepended to every activation.

**App notifications**: Files in `core/apps/<app>.md` are injected as an **App Notifications** block in your system prompt on every activation. Create, update, or remove these files directly with bash when you need persistent status displays or alerts.

**New tools/skills**: Use the `skill` tool to load `creator-mode` before building.
