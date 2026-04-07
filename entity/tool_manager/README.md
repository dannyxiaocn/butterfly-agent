# tool_manager

## Purpose
Persistent analyst for tool and skill usage across sessions. It aggregates runtime events and conversation context, produces usage reports, and highlights maintenance patterns.

## Notes
- Intended to run as a persistent background entity.
- Reads session `events.jsonl` / `context.jsonl` data and writes aggregate reporting artifacts.
- Reports should be emitted to `_sessions/tool_stats/report.md` in markdown table form.
