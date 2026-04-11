# Core — Implementation

## Files

| File | Purpose |
|------|---------|
| `agent.py` | `Agent` class — the LLM agent loop with tool execution |
| `tool.py` | `Tool` wrapper and `@tool` decorator; auto-generates JSON Schema from type hints |
| `skill.py` | `Skill` dataclass — can be inline or file-backed (progressive disclosure) |
| `provider.py` | Abstract `Provider` interface: `async complete(messages, tools, system_prompt, model)` |
| `types.py` | `Message`, `ToolCall`, `TokenUsage`, `AgentResult` dataclasses |
| `hook.py` | Callback type aliases: `OnTextChunk`, `OnToolCall`, `OnToolDone`, `OnLoopStart`, `OnLoopEnd` |
| `loader.py` | `BaseLoader[T]` abstract — `load(path)`, `load_dir(directory)` |

## Agent.run() Loop

1. **Build system prompt** via `_build_system_parts()` → `(static_prefix, dynamic_suffix)`
   - Static: `system.md` + session context (cacheable by Anthropic)
   - Dynamic: memory, memory_layers, app_notifications, agent-mode guidance, skills catalog
2. **Iterate** up to `max_iterations` (default 20):
   - Call `provider.complete()` with messages, tools, system prompt, model
   - If provider fails, try `_get_fallback_provider()` (configurable fallback)
   - If no tool_calls → break (agent is done)
   - Execute all tool calls **concurrently** via `asyncio.gather`
   - Append tool results and continue
3. **Return** `AgentResult(content, tool_calls, usage, messages, iterations)`

## Usage

```python
from nutshell.core import Agent
from nutshell.llm_engine.registry import resolve_provider

agent = Agent(provider=resolve_provider("anthropic"), model="claude-sonnet-4-6")
result = await agent.run("hello")
```

`Agent` is usually created by `session_engine`, not directly.

## Important Behaviors

- Prompt building splits into stable prefix + dynamic suffix for provider cache efficiency
- Memory layers from `core/memory/*.md` are truncated in-prompt after 60 lines, with hint to read full file via bash
- App notifications from `core/apps/*.md` injected every activation
- If `caller_type="agent"`, adds machine-oriented reply guidance for inter-agent calls
- Fallback provider: if primary `complete()` raises, transparently switches to fallback
