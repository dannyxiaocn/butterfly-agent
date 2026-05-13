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
| `playground/` | Your workspace: `tmp/` scratch, `projects/` long-term, `output/` artifacts |
| `_sessions/{session_id}/` | System internals — do not edit |

**bash default directory**: `sessions/{session_id}/` — use short relative paths.

**Develop Machine handoff**: the reviewer reads ONLY the text of your final
reply (the parent workflow does not forward intermediate tool calls,
thinking, or partial messages). Anything you want the reviewer to see —
file paths, the test command, caveats — goes in the `[DONE]` reply body.

**Triplet placement**: keep code, doc, and test commits/diffs in the
shared repo (not in `playground/`) so the merger can pick them up. Use
`playground/tmp/` only for scratch (logs, exploratory scripts).
