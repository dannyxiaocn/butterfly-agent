# Agent — Design

Agents are **reusable agent templates**. Each agent directory defines prompts, tools, skills, defaults, and seed memory used to bootstrap sessions.

## Design Principles

- Agents are the **configuration layer above the runtime code**
- The same runtime instantiates different agent behaviors by pointing at different agents
- Each agent is **fully self-contained** — all prompts, tools, and skills are explicitly listed and physically present; there is no runtime inheritance
- An agent is the **initial seed** for a meta session; after seeding, the meta session evolves independently

## Agent Lifecycle

```
agenthub/<name>/          ← static, version-controlled template
  → populate_meta_from_agent()  ← once, at meta session creation
    → sessions/<name>_meta/      ← authoritative living config
      → init_session()           ← each new child session
        → sessions/<id>/         ← child session
```

## Creating a New Agent

Use `butterfly agent new`:

```bash
butterfly agent new -n my-agent                    # defaults to --init-from agent
butterfly agent new -n my-agent --init-from agent  # copy all files from 'agent'
butterfly agent new -n my-agent --blank            # empty placeholder files
```

`--init-from` performs a one-time full copy (prompts, tools, skills, config.yaml with updated name).
There is no live link — the copy is independent from the moment it is created.

## Relationship to Meta Session

- Agent is used **once** when a meta session is first created (`populate_meta_from_agent`)
- After that, the meta session is the source of truth for all child sessions
- Meta session improvements are synced back to `agenthub/` via PRs on the `mecam/agent-update` branch
- Agent version is tracked in `agent_version` inside `config.yaml`; meta session version is in `core/config.yaml`
