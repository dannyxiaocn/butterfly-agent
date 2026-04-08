# `entity/agent/prompts`

These are the base prompt files copied into each new session's `core/` directory.

## Files

- `system.md`: default operating rules for a Nutshell agent.
- `heartbeat.md`: what to do on autonomous wake-up.
- `session.md`: explains the on-disk session layout and working conventions.

## How To Use This Part

Edit these files when you want to change the default behavior of all entities that inherit from `agent`.

## How It Contributes To The Whole System

These prompts define the baseline runtime behavior that `Session` injects into `Agent` on every activation.

