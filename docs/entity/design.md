# Entity — Design

Entities are **reusable agent templates**. Each entity directory defines prompts, tools, skills, defaults, and seed memory used to bootstrap sessions.

## Design Principles

- Entities are the **configuration layer above the runtime code**
- The same runtime instantiates different agent behaviors by pointing at different entities
- Each entity is **fully self-contained** — all prompts, tools, and skills are explicitly listed and physically present; there is no runtime inheritance
- An entity is the **initial seed** for a meta session; after seeding, the meta session evolves independently

## Entity Lifecycle

```
entity/<name>/          ← static, version-controlled template
  → populate_meta_from_entity()  ← once, at meta session creation
    → sessions/<name>_meta/      ← authoritative living config
      → init_session()           ← each new child session
        → sessions/<id>/         ← child session
```

## Creating a New Entity

Use `nutshell entity new`:

```bash
nutshell entity new -n my-agent                    # defaults to --init-from agent
nutshell entity new -n my-agent --init-from agent  # copy all files from 'agent'
nutshell entity new -n my-agent --blank            # empty placeholder files
```

`--init-from` performs a one-time full copy (prompts, tools, skills, agent.yaml with updated name).
There is no live link — the copy is independent from the moment it is created.

## Relationship to Meta Session

- Entity is used **once** when a meta session is first created (`populate_meta_from_entity`)
- After that, the meta session is the source of truth for all child sessions
- Meta session improvements are synced back to `entity/` via PRs on the `mecam/entity-update` branch
- Entity version is tracked in `agent_version` inside `agent.yaml`; meta session version is in `core/params.json`
