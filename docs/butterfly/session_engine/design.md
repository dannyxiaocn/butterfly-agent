# Session Engine — Design

The session engine is the **bridge between static entity definitions and live runtime sessions**.

## Responsibilities

- Parse entity `config.yaml` manifests
- Build `Agent` objects from fully self-contained entity directories
- Manage meta sessions (entity → meta → child session lifecycle)
- Create session directory structures on disk
- Wrap `Agent` with persistent, file-backed `Session` behavior
- Track agent versions and notify stale sessions

## Key Concepts

### Entity → Meta → Session Flow

```
entity/<name>/          ← static template, version-controlled
  → populate_meta_from_entity()  ← one-time seed at meta session creation
    → sessions/<entity>_meta/   ← authoritative living config (evolves independently)
      → init_session()           ← each new child session
        → sessions/<id>/         ← child session (seeded from meta, then independent)
```

### Meta Sessions

Each entity has a meta session (`<entity>_meta`) that:
- Holds the canonical, evolving config for all future child sessions of that entity
- Acts as shared mutable state store (memory, playground)
- Runs as a real persistent agent with "dream cycle" task schedule
- Maintains `agent_version` in `core/config.yaml`
- Syncs improvements back to `entity/` via PRs on the `mecam/entity-update` branch

### Entity Templates

`entity/<name>/` is a **static seed**, not a live config:
- Used once to bootstrap the meta session
- Each entity is fully self-contained — all prompts, tools, skills are physically present
- `init_from` in `config.yaml` documents provenance but has no runtime effect
- New entities are created with `butterfly entity new --init-from <source>` (one-time copy) or `--blank`

### Version Staleness Notices

When a child session starts its daemon loop, it compares its `agent_version` against the meta session's current version. If meta has advanced, a `system_notice` event is emitted — rendered in both web UI and CLI — suggesting the user start a new session to pick up the latest configuration.
