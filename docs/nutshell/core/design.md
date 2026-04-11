# Core — Design

The core is the **pure computation layer** of nutshell. It defines the agent loop and shared abstractions with zero IO, zero scheduling, and zero lifecycle management.

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
