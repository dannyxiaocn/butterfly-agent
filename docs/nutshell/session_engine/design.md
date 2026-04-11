# Session Engine — Design

The session engine is the **bridge between static entity definitions and live runtime sessions**.

## Responsibilities

- Parse entity `agent.yaml` manifests with inheritance
- Build `Agent` objects from entity directories (resolving extends chains)
- Manage meta sessions (entity → meta → child session lifecycle)
- Create session directory structures on disk
- Wrap `Agent` with persistent, file-backed `Session` behavior
- Handle entity/meta alignment detection and resolution

## Key Concepts

### Entity → Meta → Session Flow

```
entity/<name>/agent.yaml
  → populate_meta_from_entity() flattens config
    → sessions/<entity>_meta/ (the "ground truth")
      → init_session() copies from meta
        → sessions/<id>/ (child session)
```

### Meta Sessions

Each entity has a meta session (`<entity>_meta`) that:
- Holds flattened, inherited config as the canonical template
- Acts as shared mutable state store (memory, playground)
- Runs as a real persistent agent with "dream cycle" heartbeat
- Has alignment checking to detect entity↔meta drift

### Entity Inheritance

`agent.yaml` supports single inheritance via `extends`:
- `link`: fields inherited from parent (stay in sync)
- `own`: fields this entity defines independently
- `append`: fields where child values are appended to parent values
