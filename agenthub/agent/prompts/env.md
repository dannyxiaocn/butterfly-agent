---
## Session Files

Your session: `sessions/{session_id}/`

| Path | Purpose |
|------|---------|
| `core/tasks/` | Task cards. Each task = `<name>.json` + `<name>.trigger.sh` + optional `<name>.end.sh`. |
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

**bash default directory**: `sessions/{session_id}/` — use short relative paths: `ls core/tasks`, `cat core/tasks/duty.trigger.sh`, `ls playground/`. Use `workdir=...` to override per call.

**Task cards (bash-driven triggers)**: The runtime polls `core/tasks/<name>.trigger.sh` every `check_interval` seconds. The LAST line of stdout decides what happens:

- `[start]` — activate me now
- `[start] <message>` — activate me now; `<message>` becomes the seed input
- `[skip]` — not yet; check again next interval

Non-zero exit / unrecognised output is fail-closed (treated as `[skip]`, logged as error). Optional `core/tasks/<name>.end.sh` is polled while you're running and ends the card on `[done]`. Use `task_create` / `task_update` tools to author scripts, or edit the `.sh` files directly with bash. Update cards with progress notes your future self can resume from.

**External hooks**: Drop `core/hook/<event>/main.sh` to react to session events. stdin is a JSON envelope `{event, session_id, data}`; working directory is the session root. 30 s timeout, observe-only (exit code is logged but doesn't block). Events: `session_start` (daemon startup), `agent_loop_start` (before every Agent.run — chats, task wakeups, background notifications), `agent_loop_end` (after — `data.reason` ∈ {`finished`, `cancelled`, `error`}). Write helper scripts anywhere you like; call them from `main.sh`.

**Memory**: One fact per line. Avoid injecting large documents — memory is prepended to every activation.

**App notifications**: Files in `core/apps/<app>.md` are injected as an **App Notifications** block in your system prompt on every activation. Create, update, or remove these files directly with bash when you need persistent status displays or alerts.

**New tools/skills**: Use the `skill` tool to load `creator-mode` before building.
