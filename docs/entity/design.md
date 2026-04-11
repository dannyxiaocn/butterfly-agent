# Entity — Design

Entities are **reusable agent templates**. Each entity directory defines prompts, tools, skills, defaults, and seed memory used to create sessions.

## Design Principles

- Entities are the **configuration layer above the runtime code**
- The same runtime instantiates different agent behaviors by pointing at different entities
- Entities support single inheritance via `extends` in `agent.yaml`
- Each entity has a meta session that holds the flattened config as "ground truth"

## Inheritance Chain

```
agent (base, standalone)
  └── nutshell_dev (extends agent)
        └── nutshell_dev_codex (extends nutshell_dev)
```

## Inheritance Semantics

| Keyword | Meaning |
|---------|---------|
| `extends` | Parent entity name |
| `own` | Fields this entity defines independently |
| `link` | Fields inherited by reference from parent |
| `append` | Fields where child values are appended to parent |
