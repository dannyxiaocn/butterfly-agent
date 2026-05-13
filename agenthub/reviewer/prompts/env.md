---
## Session Files

Your session: `sessions/{session_id}/`

| Path | Purpose |
|------|---------|
| `core/tasks/` | Task cards. Each task = `<name>.json` + `<name>.sh`. |
| `core/hook/` | External hooks: `session_start/main.sh`, `agent_loop_start/main.sh`, `agent_loop_end/main.sh`. |
| `core/memory.md` | Persistent memory — injected every activation. Keep concise. |
| `core/apps/` | App notifications (`<app>.md` files, injected into system prompt each activation) |
| `core/skills/` | Session skills (`<name>/SKILL.md`, reload on activation) |
| `core/tools/` | Session tools (`.json` + `.sh` pairs, reload on activation) |
| `core/config.yaml` | Runtime config: `model`, `provider`, thinking |
| `core/system.md` | Your system prompt (editable, effective next activation) |
| `core/task.md` | Your task prompt (editable, effective next activation) |
| `docs/` | User files — read-only |
| `playground/` | Your workspace: `tmp/` scratch for review notes |
| `_sessions/{session_id}/` | System internals — do not edit |

**bash default directory**: `sessions/{session_id}/` — use short relative paths.

**Develop Machine handoff**: the merger reads ONLY the text of your final
reply. Anything you want the merger to act on — `git` invocations to run,
commits to verify, files to re-check — must appear in the `[APPROVE]`
body. Don't assume any prior context survives.

**Read access**: in `explorer` mode you can read the whole repo freely;
writes are confined to `playground/`. Use `playground/tmp/` for any
scratch notes you want to keep across activations.
