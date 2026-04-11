# nutshell_dev — Implementation

## Usage

```bash
nutshell new --entity nutshell_dev
nutshell chat --entity nutshell_dev "implement the next track.md item"
```

## What It Adds Over agent

- Custom heartbeat for autonomous repo task selection
- `nutshell` skill with project-specific development guidance
- Seed memory with project identity and workflow

## Branch Policy

- `wip-<slug>` for active implementation
- `ready-<slug>` for porter/merge review
