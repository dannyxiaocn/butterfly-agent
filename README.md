# Nutshell

A minimal Python Agent library. Simple by design.

```python
from nutshell import Agent, AnthropicProvider, tool

@tool(description="Add two integers")
def add(a: int, b: int) -> int:
    return a + b

agent = Agent(
    system_prompt="You are a helpful assistant.",
    tools=[add],
    model="claude-opus-4-6",
    provider=AnthropicProvider(),
)

result = await agent.run("What is 17 + 25?")
print(result.content)
```

---

## Install

```bash
pip install anthropic          # Anthropic Claude
pip install openai             # optional: OpenAI support
pip install pytest pytest-asyncio  # for tests
```

---

## Core Concepts

### Agent

The central class. Configured with a system prompt, tools, skills, and a provider.

```python
agent = Agent(
    system_prompt="You are a concise assistant.",
    tools=[...],
    skills=[...],
    model="claude-opus-4-6",
    provider=AnthropicProvider(),
    release_policy="persistent",  # "auto" | "manual" | "persistent"
    max_iterations=20,
)
result = await agent.run("Hello")
```

**Conversation history** is maintained across `.run()` calls by default (`release_policy="persistent"`). Use `clear_history=True` to reset mid-session:

```python
result = await agent.run("new topic", clear_history=True)
```

### Tool

External actions that execute outside the LLM loop (API calls, file I/O, calculations).

```python
from nutshell import tool

@tool(description="Search the web for a query")
async def search(query: str) -> str:
    ...

# Or construct manually
from nutshell import Tool

my_tool = Tool(
    name="search",
    description="Search the web",
    func=search_func,
)
```

The `@tool` decorator automatically generates a JSON Schema from the function's type annotations.

### Skill

Injects knowledge or behavior into the agent's system prompt.

```python
from nutshell import Skill

coding_skill = Skill(
    name="coding",
    description="Expert coding assistant",
    prompt_injection="You are an expert programmer. Always write clean, idiomatic code.",
)

agent = Agent(skills=[coding_skill], ...)
```

Each skill's `prompt_injection` is appended to `system_prompt` at runtime.

### Provider

Pluggable LLM backend. Implement `Provider.complete()` to add any model.

```python
from nutshell import AnthropicProvider
from nutshell.providers.openai import OpenAIProvider

provider = AnthropicProvider(api_key="sk-...")   # or reads ANTHROPIC_API_KEY
provider = OpenAIProvider(api_key="sk-...")       # or reads OPENAI_API_KEY
```

**Custom provider:**

```python
from nutshell import Provider

class MyProvider(Provider):
    async def complete(self, messages, tools, system_prompt, model):
        # Return (content: str, tool_calls: list[ToolCall])
        ...
```

---

## Multi-Agent Patterns

### Pattern A: Agent-as-Tool

Sub-agents registered as tools of a parent agent.

```python
writer = Agent(
    system_prompt="You are a creative writer.",
    release_policy="auto",  # history cleared after each tool call
)

orchestrator = Agent(
    system_prompt="You coordinate other agents.",
    tools=[writer.as_tool("write_paragraph", "Write a short paragraph on a topic.")],
)

result = await orchestrator.run("Write a paragraph about the ocean.")
```

### Pattern B: Message Passing

Manual pipeline — output of one agent feeds into another.

```python
research = await researcher.run("Key facts about black holes")
summary  = await summarizer.run(research.content)
```

### Sub-agent Lifecycle

Control history with `release_policy`:

| Policy | Behavior |
|--------|----------|
| `"persistent"` | History kept across all runs (default) |
| `"auto"` | History cleared after each `run()` |
| `"manual"` | History cleared only when `.close()` is called |

---

## AgentResult

Every `.run()` returns an `AgentResult`:

```python
result.content      # str: final assistant response
result.tool_calls   # list[ToolCall]: all tool calls made this run
result.messages     # list[Message]: full conversation history
```

---

## Design

### Architecture

```
Agent
  ├── system_prompt          → defines identity and behavior
  ├── tools: list[Tool]      → external actions (outside LLM loop)
  ├── skills: list[Skill]    → injected knowledge (inside LLM reasoning)
  ├── provider: Provider     → pluggable LLM backend
  └── run(input) -> AgentResult

Tool                          Skill
  ├── name                     ├── name
  ├── description              ├── description
  ├── schema (JSON Schema)     └── prompt_injection → appended to system_prompt
  └── execute(**kwargs)

Provider (ABC)
  ├── AnthropicProvider
  └── OpenAIProvider
```

### Execution Loop

```
run(input)
  │
  ├── 1. Build system_prompt
  │       = agent.system_prompt
  │       + skill.prompt_injection (for each skill)
  │
  ├── 2. provider.complete(messages, tools, system_prompt, model)
  │
  ├── 3. Check response
  │   ├── no tool_calls → return AgentResult
  │   └── tool_calls   → execute tools concurrently
  │                       → append results → go to step 2
  │
  └── 4. Return AgentResult (update history per release_policy)
```

### Tool vs Skill

| | Tool | Skill |
|-|------|-------|
| **Runs** | Outside LLM loop | Inside LLM reasoning |
| **Purpose** | Execute actions (API, I/O) | Inject domain expertise |
| **Mechanism** | LLM calls it by name | Appended to system prompt |
| **Examples** | web search, calculator | coding expert, formatter |

### Design Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Language | Python | Widest AI ecosystem |
| API style | Object config | No inheritance required |
| LLM provider | Pluggable ABC | Any model can be added |
| Agent connections | as_tool + message passing | Covers hierarchical and pipeline patterns |
| Sub-agent lifecycle | release_policy | Flexible memory management |

---

## Project Structure

```
nutshell/
├── agent.py         # Agent class + execution loop
├── tool.py          # Tool class + @tool decorator
├── skill.py         # Skill class
├── provider.py      # Provider ABC
├── providers/
│   ├── anthropic.py
│   └── openai.py
└── types.py         # Message, ToolCall, AgentResult

examples/
├── 01_basic_agent.py
├── 02_custom_tools.py
└── 03_multi_agent.py

tests/
├── test_agent.py
└── test_tools.py
```

---

## Tests

```bash
pytest tests/
```

Tests use a `MockProvider` — no API key required.
