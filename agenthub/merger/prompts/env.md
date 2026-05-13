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
| `playground/` | Your workspace: `tmp/` scratch for merge notes |
| `_sessions/{session_id}/` | System internals — do not edit |

**bash default directory**: `sessions/{session_id}/` — use short relative paths.

**Develop Machine handoff**: your final reply is the workflow's output —
the user (or the wrapping orchestrator agent) reads it as the audit
trail for this triplet. Include enough detail that someone can verify
your push without re-running the workflow.

**Repo work**: you operate `executor` mode, full write access to the
repo. Stage files individually (`git add <path>`), never `git add -A`.
Never push to a branch you didn't already see in `git branch -a`.
