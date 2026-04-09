# `entity/nutshell_dev/prompts`

This entity overrides only the heartbeat prompt.

## File

- `heartbeat.md`: it treats `core/tasks/heartbeat.md` as the recurring repo-work task card, selecting the next actionable `track.md` item when that card is empty and continuing the current repo task otherwise.

## How It Contributes To The Whole System

This prompt is what makes `nutshell_dev` autonomous for this repository rather than just a generic persistent assistant.
