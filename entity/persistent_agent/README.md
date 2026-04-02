# persistent_agent

Always-on background utility agent.

## Purpose
- long-lived session that periodically wakes to inspect messages and state
- suitable for maintenance, monitoring, or lightweight coordination duties

## Notes
- extends `agent`
- mainly differentiated by `params.persistent`, default task, and long heartbeat
