# Butterfly — Design

Butterfly is a file-backed Python agent runtime. The design follows these principles:

1. **`core/` is the cleanest agent loop** — Provider, Tool, Skill, Hook, and the iteration over them. Zero IO, zero scheduling, zero lifecycle.
2. **Engines fill the loop's slots** — `llm_engine` → Provider, `tool_engine` → Tools, `skill_engine` → Skills, `session_engine` → Session wrapping the agent loop.
3. **`runtime/` is the central coordinator** — watches sessions on disk, starts daemons, provides file-based IPC.
4. **`agenthub/` is assets** — read-only config (prompts, tools, skills) seeded into sessions at creation.
5. **Filesystem as agent's backend** — agents read/write their session dir; UI and server communicate via `context.jsonl` + `events.jsonl`. No sockets, no databases.
6. **CLI is the primary user interface**.
## Layer Diagram

```
agenthub/ (static agent templates)
  → session_engine (materializes agents into sessions)
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
- **Self-contained agents**: Each agent in `agenthub/` is fully self-contained — all prompts, tools, and skills are physically present. New agents are created with `--init-from <source>` (one-time copy) or `--blank`.
- **Meta sessions**: Each agent seeds a meta session once; the meta session is the authoritative, evolving config. Child sessions are seeded from meta. Version staleness notices inform users when meta has advanced.
- **File-based IPC**: JSONL append-only logs with byte-offset polling. No sockets, no message queues.

## Versioning

Version format: **`{major}.{stable_minor}.{dev_patch}`**.

- **`major`** = `stable_major + 1`. Dev is always exactly one major ahead of the latest `stable_v{major}` git tag. Today: stable tag is `stable_v1`, so dev major is `2`.
- **`stable_minor`** only increments when a dev release is promoted to stable and gets tagged `stable_v{major}.{minor}` (e.g. `stable_v1.1`). Until that paired event happens, it stays at the value it had at the last stable cut. Today: `0`.
- **`dev_patch`** increments on every dev release between stable cuts. Each PR ships as `vX.Y.Z+1`.

Practical rules:
- Day-to-day PRs bump only the last number (`v2.0.3 → v2.0.4`), even for big features.
- Bumping the middle number is paired with cutting a new `stable_v{major}.{minor}` tag — never on its own.
- Major never moves in dev; it advances when the upstream stable major tag advances.
- `pyproject.toml` `version = ...` must match the dev version after every PR.
