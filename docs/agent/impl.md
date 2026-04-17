# Agent — Implementation

## Agent Directory Structure

```
agenthub/<name>/
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
butterfly new --agent agent                        # Create session from agent
butterfly chat --agent butterfly_dev "fix the failing test"
butterfly agent new -n myagent --init-from agent  # Copy agent from 'agent'
butterfly agent new -n myagent --blank            # Blank agent with empty files
```

## Session Creation from Agent

1. Agent definition → `populate_meta_from_agent()` → meta session `sessions/<agent>_meta/`
2. Meta session → `init_session()` copies config → child session `sessions/<id>/`
3. Child sessions get their config from **meta session**, not directly from agent

## Key config.yaml Fields

| Field | Purpose |
|-------|---------|
| `name` | Agent identifier |
| `version` | Agent version string (bumped when agent is updated from meta) |
| `init_from` | Source agent this was initialized from (documentation only, no runtime effect) |
| `model` / `provider` | Default LLM |
| `prompts` | Map of role → file path |
| `tools` | List of tool JSON schema paths |
| `skills` | List of skill directory paths |
| `params` | Extra params merged into `core/config.yaml` |
| `meta_session` | Description of the agent's meta session, surfaced to the meta agent on wake-up |

## Important Files

- `config.yaml`: the manifest — everything flows from here
- `prompts/`: system, task, env prompt files
- `tools/`: default tool schemas
- `skills/`: default skill catalog
- `memory.md` and `memory/*.md`: seed memory copied into new sessions
