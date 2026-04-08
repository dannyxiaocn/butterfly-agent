# `entity/`

Entities are reusable agent templates. Each entity directory defines the prompts, tools, skills, defaults, and seed memory used to create sessions.

## Built-In Entities

- `agent/`: the default base entity.
- `nutshell_dev/`: a repo-development variant for this project.
- `nutshell_dev_codex/`: the Codex-tuned development variant.

## How To Use It

```bash
nutshell new --entity agent
nutshell chat --entity nutshell_dev "fix the failing test"
```

Each new child session is created from:

1. the entity definition
2. the entity's meta session (`sessions/<entity>_meta/`)
3. session-specific files created under `sessions/<id>/`

## How It Contributes To The Whole System

- It is the configuration layer above the runtime code.
- It lets the same runtime instantiate different agent behaviors without changing Python code.
- It is also where durable improvements for future sessions should live.

## Important Files

- `agent.yaml`: entity manifest and defaults.
- `prompts/`: system, heartbeat, and session prompt files.
- `tools/`: default tool schemas exposed to sessions.
- `skills/`: default skill catalog.
- `memory.md` and `memory/*.md`: seed memory copied into new sessions.

