# Core — Design

The core is the **pure computation layer** of butterfly. It defines the agent loop and shared abstractions with zero IO, zero scheduling, and zero lifecycle management.

## Responsibilities

- Define the `Agent` run loop: prompt → LLM → tool calls → repeat
- Define interfaces: `Provider`, `Tool`, `Skill`, `Hook`
- Define shared types: `Message`, `ToolCall`, `TokenUsage`, `AgentResult`
- Provide the `BaseLoader[T]` generic for higher layers

## Design Constraints

- **No file IO**: core never reads from or writes to disk
- **No scheduling**: no timers, no event loops, no daemon logic
- **No lifecycle**: core does not manage session state or persistence
- Higher layers (`session_engine`, `llm_engine`, `tool_engine`, `skill_engine`) inject concrete implementations into the core's slots

## `Agent.run()` history-commit cadence (v2.0.12)

`run()` writes `self._history = list(messages)` at three points per iteration: after the assistant content append, after the tool-results append, and once more at the natural end of the loop. The trailing assignment is now redundant for non-cancelled runs — the per-iteration commits already mirror `messages` to `_history`. Per-iteration commits are what let `Session._dispatch_one` decide between two cancellation responses: if `len(history)` did not move past the baseline, no LLM response was committed and the cancelled input is folded into the next chat; if it grew, at least one assistant turn was committed, the dispatcher saves a partial `interrupted: True` turn, and the new chat runs with a fresh user message.

The agent layer itself stays free of dispatcher knowledge: it just guarantees history reflects every committed iteration, so any orchestrator can read the post-cancellation state.
