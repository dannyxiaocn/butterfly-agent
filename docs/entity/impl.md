# Entity — Implementation

## Entity Directory Structure

```
entity/<name>/
├── config.yaml         # Manifest: name, version, init_from, model, provider, tools, skills, prompts, params
├── prompts/
│   ├── system.md       # System prompt
│   ├── task.md        # Task instructions
│   └── env.md          # Session context template
├── tools/              # Tool JSON schemas
├── skills/             # Skill directories (each with SKILL.md)
├── memory.md           # Seed memory
└── memory/             # Layered memory files
```

## CLI Usage

```bash
butterfly new --entity agent                        # Create session from entity
butterfly chat --entity butterfly_dev "fix the failing test"
butterfly entity new -n myentity --init-from agent  # Copy entity from 'agent'
butterfly entity new -n myentity --blank            # Blank entity with empty files
```

## Session Creation from Entity

1. Entity definition → `populate_meta_from_entity()` → meta session `sessions/<entity>_meta/`
2. Meta session → `init_session()` copies config → child session `sessions/<id>/`
3. Child sessions get their config from **meta session**, not directly from entity

## Key config.yaml Fields

| Field | Purpose |
|-------|---------|
| `name` | Entity identifier |
| `version` | Entity version string (bumped when entity is updated from meta) |
| `init_from` | Source entity this was initialized from (documentation only, no runtime effect) |
| `model` / `provider` | Default LLM |
| `prompts` | Map of role → file path |
| `tools` | List of tool JSON schema paths |
| `skills` | List of skill directory paths |
| `params` | Extra params merged into `core/config.yaml` |
| `meta_session` | Description of the entity's meta session, surfaced to the meta agent on wake-up |

## Important Files

- `config.yaml`: the manifest — everything flows from here
- `prompts/`: system, task, env prompt files
- `tools/`: default tool schemas
- `skills/`: default skill catalog
- `memory.md` and `memory/*.md`: seed memory copied into new sessions
