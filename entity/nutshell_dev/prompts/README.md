# `entity/nutshell_dev/prompts`

This entity overrides only the heartbeat prompt.

## File

- `heartbeat.md`: when `core/tasks.md` is empty, it selects the next actionable item from `track.md`; otherwise it continues the current repo task.

## How It Contributes To The Whole System

This prompt is what makes `nutshell_dev` autonomous for this repository rather than just a generic persistent assistant.

