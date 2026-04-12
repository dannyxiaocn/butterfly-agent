# Entity — Implementation

## Entity Directory Structure

```
entity/<name>/
├── agent.yaml          # Manifest: name, version, init_from, model, provider, tools, skills, prompts, params
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
nutshell new --entity agent                        # Create session from entity
nutshell chat --entity nutshell_dev "fix the failing test"
nutshell entity new -n myentity --init-from agent  # Copy entity from 'agent'
nutshell entity new -n myentity --blank            # Blank entity with empty files
```

## Session Creation from Entity

1. Entity definition → `populate_meta_from_entity()` → meta session `sessions/<entity>_meta/`
2. Meta session → `init_session()` copies config → child session `sessions/<id>/`
3. Child sessions get their config from **meta session**, not directly from entity

## Key agent.yaml Fields

| Field | Purpose |
|-------|---------|
| `name` | Entity identifier |
| `version` | Entity version string (bumped when entity is updated from meta) |
| `init_from` | Source entity this was initialized from (documentation only, no runtime effect) |
| `model` / `provider` | Default LLM |
| `prompts` | Map of role → file path |
| `tools` | List of tool JSON schema paths |
| `skills` | List of skill directory paths |
| `params` | Extra params merged into `core/params.json` |
| `meta_session` | Description shown in `nutshell meta` output |

## Important Files

- `agent.yaml`: the manifest — everything flows from here
- `prompts/`: system, heartbeat, session prompt files
- `tools/`: default tool schemas
- `skills/`: default skill catalog
- `memory.md` and `memory/*.md`: seed memory copied into new sessions
