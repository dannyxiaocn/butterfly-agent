# Service — Implementation

## Modules

| Module | Purpose |
|--------|---------|
| `sessions_service.py` | Session discovery, create, stop, start, delete |
| `messages_service.py` | Enqueue user messages, wait for reply, iterate events, interrupt |
| `history_service.py` | Log turns, pending inputs, prompt stats, token reports |
| `tasks_service.py` | Task card CRUD |
| `config_service.py` | Session params/config get and update |
| `hud_service.py` | HUD summary data for web UI |

## Usage

```python
from nutshell.service.sessions_service import create_session, list_sessions
from nutshell.service.messages_service import send_message
```

CLI and Web should only call these functions, never access IPC/status files directly.
