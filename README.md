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
pip install -e .              # installs anthropic + pyyaml
pip install openai            # optional: OpenAI support
pip install pytest pytest-asyncio  # for tests
```

---

## Three-Layer Architecture

Nutshell is organized into three distinct layers:

```
Layer 1 — nutshell/          Core framework: abstract base classes, concrete implementations, file loaders
Layer 2 — nutshell/infra/    Agent scheduling infrastructure (placeholder, not yet implemented)
Layer 3 — entity/            Agent content: prompt files, tool schemas, skill definitions
```

### Layer 1: Core Framework (`nutshell/`)

The library itself. Contains:
- **`base/`** — Pure abstract base classes (ABCs) defining the interfaces for all agent system components
- **`loaders/`** — File loaders that read external configuration into Python objects
- **`agent.py`, `tool.py`, `skill.py`** — Concrete implementations that extend the ABCs
- **`providers/`** — Pluggable LLM backends (Anthropic, OpenAI)

### Layer 2: Infrastructure (`nutshell/infra/`)

Placeholder for future agent scheduling capabilities:
- Task queue management
- Concurrency limits across agent pool
- Retry and timeout policies
- Priority-based dispatch

### Layer 3: Entity (`entity/`)

Agent content stored as plain files — no Python required:

| Configuration | Format | Extension |
|---|---|---|
| System prompt | Plain Markdown | `.md` |
| Tool schema | JSON Schema (Anthropic/OpenAI format) | `.json` |
| Skill | YAML frontmatter + Markdown body | `.md` |
| Agent manifest | YAML | `.yaml` |

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

my_tool = Tool(name="search", description="Search the web", func=search_func)
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

---

## External File Loaders

Load agent configuration from files instead of hardcoding in Python.

### PromptLoader — `.md` → `str`

```python
from nutshell.loaders import PromptLoader

system_prompt = PromptLoader().load(Path("entity/core_agent/prompts/system.md"))
```

### SkillLoader — `.md` (YAML frontmatter) → `Skill`

Skill files use YAML frontmatter for metadata and Markdown body for the prompt content:

```markdown
---
name: reasoning
description: Encourages step-by-step reasoning.
---

Before answering, work through your reasoning explicitly...
```

```python
from nutshell.loaders import SkillLoader

skills = SkillLoader().load_dir(Path("entity/core_agent/skills/"))
```

### ToolLoader — `.json` → `Tool`

Tool files use Anthropic-compatible JSON Schema format:

```json
{
  "name": "echo",
  "description": "Return input text unchanged.",
  "input_schema": {
    "type": "object",
    "properties": { "text": { "type": "string" } },
    "required": ["text"]
  }
}
```

Python implementations are wired in separately:

```python
from nutshell.loaders import ToolLoader

loader = ToolLoader(impl_registry={"echo": lambda text: text})
tools = loader.load_dir(Path("entity/core_agent/tools/"))
```

### Full Example — Load Agent from `entity/`

```python
from pathlib import Path
from nutshell import Agent, AnthropicProvider
from nutshell.loaders import PromptLoader, SkillLoader, ToolLoader

AGENT_DIR = Path("entity/core_agent")

system_prompt = PromptLoader().load(AGENT_DIR / "prompts" / "system.md")
skills = SkillLoader().load_dir(AGENT_DIR / "skills")
tools = ToolLoader(impl_registry={"echo": lambda text: text}).load_dir(AGENT_DIR / "tools")

agent = Agent(system_prompt=system_prompt, tools=tools, skills=skills,
              model="claude-haiku-4-5-20251001", provider=AnthropicProvider())
```

See `examples/05_entity_agent.py` for the full runnable example.

---

## Abstract Base Classes

Extend these to build custom implementations:

```python
from nutshell import BaseAgent, BaseTool, BaseSkill, BaseLoader

class MyAgent(BaseAgent):
    async def run(self, input: str, *, clear_history: bool = False) -> AgentResult: ...
    def close(self) -> None: ...

class MyTool(BaseTool):
    async def execute(self, **kwargs) -> str: ...
    def to_api_dict(self) -> dict: ...

class MySkill(BaseSkill):
    def to_prompt_fragment(self) -> str: ...

class MyLoader(BaseLoader[MyTool]):
    def load(self, path: Path) -> MyTool: ...
    def load_dir(self, directory: Path) -> list[MyTool]: ...
```

---

## Multi-Agent Patterns

### Pattern A: Agent-as-Tool

Sub-agents registered as tools of a parent agent.

```python
writer = Agent(system_prompt="You are a creative writer.", release_policy="auto")

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
┌─────────────────────────────────────────────────────────┐
│  Layer 1: nutshell/ (Core Framework)                    │
│                                                         │
│  base/           loaders/        concrete impls         │
│  ├── BaseAgent   ├── PromptLoader  ├── Agent            │
│  ├── BaseTool    ├── ToolLoader    ├── Tool              │
│  ├── BaseSkill   └── SkillLoader   ├── Skill            │
│  └── BaseLoader                   └── providers/       │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│  Layer 2: nutshell/infra/ (Placeholder)                 │
│  └── Scheduler (stub — not yet implemented)             │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│  Layer 3: entity/ (Agent Content)                       │
│  └── core_agent/                                        │
│      ├── agent.yaml          ← agent manifest           │
│      ├── prompts/system.md   ← system prompt            │
│      ├── tools/echo.json     ← tool schema              │
│      └── skills/reasoning.md ← skill definition        │
└─────────────────────────────────────────────────────────┘
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
| **Config format** | `.json` (JSON Schema) | `.md` (YAML frontmatter + body) |
| **Examples** | web search, calculator | coding expert, step-by-step reasoning |

---

## Project Structure

```
nutshell/
├── nutshell/                  # Layer 1: Core framework
│   ├── base/                  # Pure abstract base classes
│   │   ├── agent.py           # BaseAgent(ABC)
│   │   ├── tool.py            # BaseTool(ABC)
│   │   ├── skill.py           # BaseSkill(ABC)
│   │   └── loader.py          # BaseLoader(ABC, Generic[T])
│   ├── loaders/               # External file loaders
│   │   ├── prompt.py          # PromptLoader: .md → str
│   │   ├── tool.py            # ToolLoader: .json → Tool
│   │   └── skill.py           # SkillLoader: .md+frontmatter → Skill
│   ├── infra/                 # Layer 2: Scheduling (placeholder)
│   │   └── scheduler.py       # Scheduler stub
│   ├── agent.py               # Agent(BaseAgent)
│   ├── tool.py                # Tool(BaseTool) + @tool decorator
│   ├── skill.py               # Skill(BaseSkill)
│   ├── provider.py            # Provider ABC
│   ├── providers/
│   │   ├── anthropic.py
│   │   └── openai.py
│   └── types.py               # Message, ToolCall, AgentResult
│
├── entity/                    # Layer 3: Agent content (plain files)
│   └── core_agent/
│       ├── agent.yaml         # Agent manifest
│       ├── prompts/system.md  # System prompt
│       ├── tools/echo.json    # Tool schema
│       └── skills/reasoning.md # Skill definition
│
├── examples/
│   ├── 01_basic_agent.py
│   ├── 02_custom_tools.py
│   ├── 03_multi_agent.py
│   ├── 04_tmp_subagent.py
│   └── 05_entity_agent.py     # Load agent from entity/ using loaders
│
└── tests/
    ├── test_agent.py
    └── test_tools.py
```

---

## Tests

```bash
pytest tests/
```

Tests use a `MockProvider` — no API key required.
