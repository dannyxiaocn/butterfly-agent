# Executors — Design

Executors are the **concrete runtimes behind tools**. They are where abstract tool definitions become real behavior — the lowest layer of the tool system.

## Categories

- **Terminal**: shell-based execution (bash, agent-authored scripts)
- **Skill**: progressive disclosure — loads skill body into context on demand
- **Web Search**: swappable search backends (Brave, Tavily)
