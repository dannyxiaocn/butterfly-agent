# Entity — Implementation

## Entity Directory Structure

```
entity/<name>/
├── agent.yaml          # Manifest: name, model, provider, extends, tools, skills, prompts, params
├── prompts/
│   ├── system.md       # System prompt
│   ├── heartbeat.md    # Heartbeat instructions
│   └── session.md      # Session context template
├── tools/              # Tool JSON schemas
├── skills/             # Skill directories (each with SKILL.md)
├── memory.md           # Seed memory
└── memory/             # Layered memory files
```

## CLI Usage

```bash
nutshell new --entity agent              # Create session from entity
nutshell chat --entity nutshell_dev "fix the failing test"
nutshell entity new -n myentity --extends agent   # Scaffold new entity
```

## Session Creation from Entity

1. Entity definition → `populate_meta_from_entity()` → meta session `sessions/<entity>_meta/`
2. Meta session → `init_session()` copies config → child session `sessions/<id>/`
3. Child sessions get their config from meta, **not** directly from entity

## Important Files

- `agent.yaml`: the manifest — everything flows from here
- `prompts/`: system, heartbeat, session prompt files
- `tools/`: default tool schemas
- `skills/`: default skill catalog
- `memory.md` and `memory/*.md`: seed memory copied into new sessions
