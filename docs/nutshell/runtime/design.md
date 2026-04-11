# Runtime — Design

The runtime is the **orchestration layer**. It watches session state on disk, starts daemons, and exposes file-based communication primitives.

## Responsibilities

- Discover sessions via `_sessions/<id>/manifest.json`
- Start/stop session daemon loops
- Provide file-based IPC (`context.jsonl` + `events.jsonl`)
- Expose client-side bridge for CLI and Web UIs
- Coordinate multi-agent workflows (CAP, git master/sub)

## Design Constraints

- No in-memory state that isn't backed by files
- Session ownership via PID in `status.json` — prevents duplicate daemons
- IPC is pure append-only JSONL with byte-offset polling — no sockets, no message queues
