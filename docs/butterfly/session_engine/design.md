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

### `init_session()` invariant — manifest.json is the watcher's discovery signal

`_sessions/<id>/manifest.json` is what `SessionWatcher._scan()` checks to decide whether to spawn a `Session` task for a given session_id. Therefore:

- **manifest.json MUST be written last** in `init_session()`, only after `sessions/<id>/core/config.yaml` (and any other required seed files) is fully populated from the entity/meta.
- If manifest.json is published early, the server-side watcher can race `init_session()` and spawn `Session(session_id)` whose `Session.__init__` calls `ensure_config(session_dir)` → that writes `DEFAULT_CONFIG` (with `model=None`, `provider=None`) into the session core before `init_session` gets a chance to copy the real config. Once the stub is on disk, the `if not session_config_path.exists()` guard inside `init_session()` would silently skip the copy, leaving the session permanently stuck on `model: null`.
- As a belt-and-braces safeguard, `init_session()` also treats a config with `model` unset/null as "still needs seeding" rather than a finished session config. This way, even if a different code path writes a stub config first, the entity's model/provider still make it onto disk.

This invariant was added in v2.0.8 after a first-run repro: `butterfly-server` daemon + `butterfly new` would consistently produce `sessions/<id>/core/config.yaml` with `model: null`.

## Memory layers — on-demand recall (v2.0.5, β)

**Change from v2.0.x**: previously, every file under `core/memory/*.md` was loaded into `Agent.memory_layers` and injected into the system prompt (with a 60-line truncation). Starting v2.0.5, sub-memory is **not** injected into the prompt. Only `core/memory.md` (main) is.

### Structure

```
sessions/<id>/core/
├── memory.md              ← main memory, always in system prompt
└── memory/
    ├── dev_sop.md         ← sub-memory layer
    ├── repo_map.md        ← sub-memory layer
    └── ...
```

### Main-memory index convention

`memory.md` contains one line per sub-memory file under a `## Memory files` section:

```markdown
## Memory files
- dev_sop: SOP the agent has to follow when developing tools/skills
- repo_map: Cached map of key modules and their responsibilities
```

The agent discovers available sub-memories by reading main memory (which is always in prompt). To access a sub-memory's full contents, the agent calls `recall_memory(name="dev_sop")`.

### Write path

Sub-memory is edited exclusively via `update_memory(name, old_string, new_string, description?)`:
- Creates `core/memory/<name>.md` on first write, applying `new_string` as initial content.
- On subsequent writes, behaves like `edit`: exact replacement with uniqueness enforcement.
- Always upserts the index line `<name>: <description>` in main memory. On first-time creation, `description` is required.

Main `memory.md` itself is edited via `edit` / `write` like any other file — no dedicated tool.

### Why this change

1. **Prompt budget** — long-running sessions accumulate sub-memory layers that would otherwise bloat the system prefix. On-demand recall keeps the static prefix small and cache-friendly.
2. **Explicit retrieval** — agent decides what it needs; no silent truncation of layers it was relying on.
3. **Index discipline** — requiring a one-line description per layer forces the agent to keep a readable map in main memory, which is what a human skim-reader also wants.

### Session impl impact (for implementers)

- `Session._load_session_capabilities` stops populating `self._agent.memory_layers` from `core/memory/*.md`. The attribute is removed from the `Agent` class.
- System-prompt assembly (in `Agent`) drops the `memory_layers` rendering block.
- `recall_memory` and `update_memory` tool executors share a `memory_dir` + `main_memory_path` context injected by `ToolLoader`.
