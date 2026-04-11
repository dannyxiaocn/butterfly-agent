# Nutshell — Design

Nutshell is a file-backed Python agent runtime. The design follows these principles:

1. **`core/` is the cleanest agent loop** — Provider, Tool, Skill, Hook, and the iteration over them. Zero IO, zero scheduling, zero lifecycle.
2. **Engines fill the loop's slots** — `llm_engine` → Provider, `tool_engine` → Tools, `skill_engine` → Skills, `session_engine` → Session wrapping the agent loop.
3. **`runtime/` is the central coordinator** — watches sessions on disk, starts daemons, provides file-based IPC.
4. **`entity/` is assets** — read-only config (prompts, tools, skills) seeded into sessions at creation.
5. **Filesystem as agent's backend** — agents read/write their session dir; UI and server communicate via `context.jsonl` + `events.jsonl`. No sockets, no databases.
6. **CLI is the primary user interface**.
## Layer Diagram

```
Entity (static templates)
  → session_engine (materializes entities into sessions)
    → Session (wraps Agent with file-backed persistence)
      → Agent (core loop: prompt → LLM → tool calls → repeat)
        → Provider (llm_engine)
        → Tools (tool_engine)
        → Skills (skill_engine)
  → runtime (watcher, IPC, bridge)
    → UI (cli, web)
```

## Key Architecture Decisions

- **Dual directory pattern**: `sessions/<id>/` (agent-visible workspace) vs `_sessions/<id>/` (system-only state). Agents never see system internals.
- **Hot reload**: Capabilities reload from disk before every agent activation. Edit files → agent picks up changes next run.
- **Entity inheritance**: Deep inheritance chain with `extends`, `link/own/append` semantics in `agent.yaml`.
- **Meta sessions**: Each entity has a meta session that holds flattened config and acts as a template for child sessions.
- **File-based IPC**: JSONL append-only logs with byte-offset polling. No sockets, no message queues.
