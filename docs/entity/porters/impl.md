# Porters — Implementation

## Usage

```bash
nutshell new --entity porters
nutshell chat --entity porters "verify ready-foo and merge it if it is clean"
```

## Configuration

- `session_type`: persistent
- `heartbeat_interval`: 10800 (3 hours — batch verification, not rapid iteration)
- Custom heartbeat prompt for porter workflow
- `default_task`: validate ready- branches, run tests, repair on wip-

## Branch Policy

- Active repair on `wip-<slug>`
- Merge-ready handoff arrives on `ready-<slug>`
