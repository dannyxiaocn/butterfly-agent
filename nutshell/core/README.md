# `nutshell/core`

Core runtime abstractions: `Agent`, `Tool`, `Skill`, `Provider`, shared types, and the main tool-call loop.

## What This Part Is

- `agent.py`: runs the model loop, executes tool calls, manages conversation history, and builds the system prompt from prompts, memory, apps, and skills.
- `tool.py`: the `Tool` wrapper and `@tool` decorator.
- `skill.py`: the `Skill` data model used by file-backed and inline skills.
- `provider.py`: the provider interface every LLM adapter implements.
- `types.py`: `Message`, `ToolCall`, `TokenUsage`, and `AgentResult`.
- `hook.py`: callback type aliases for streaming text, tool events, and loop boundaries.
- `loader.py`: generic loader base class reused by higher layers.

## How To Use It

```python
from nutshell.core import Agent
from nutshell.llm_engine.registry import resolve_provider

agent = Agent(
    provider=resolve_provider("anthropic"),
    model="claude-sonnet-4-6",
)

result = await agent.run("hello")
print(result.content)
```

`Agent` is usually created by `session_engine`, not directly by the UI.

## How It Contributes To The Whole System

- `session_engine` injects prompts, memory, tools, and skills into `Agent` before each activation.
- `llm_engine` supplies concrete `Provider` implementations.
- `tool_engine` and `skill_engine` load filesystem assets into `Tool` and `Skill` objects that `Agent` can consume.
- `runtime` never reimplements model logic; it only schedules `Session`, which delegates to `Agent`.

## Important Behavior

- Prompt building is split into a stable prefix and dynamic suffix so providers can cache the stable part.
- Memory layers from `core/memory/*.md` are truncated in-prompt after 60 lines, with a hint to read the full file via `bash`.
- App notifications from `core/apps/*.md` are injected into the system prompt every activation.
- If `caller_type="agent"`, `Agent` adds machine-oriented reply guidance for inter-agent calls.

