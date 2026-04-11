# CLI — Implementation

## Files

| File | Purpose |
|------|---------|
| `main.py` | Subcommand registration and top-level orchestration |
| `chat.py` | Legacy single-shot chat helper for `nutshell chat` |
| `new_agent.py` | Entity scaffolding |
| `friends.py`, `kanban.py`, `visit.py` | Read-only session views |
| `repo_skill.py` | Repo overview skill generation |

## Common Commands

```bash
nutshell chat "message"
nutshell new --entity agent
nutshell sessions
nutshell log <id>
nutshell prompt-stats <id>
nutshell token-report <id>
nutshell meta <entity>
```
