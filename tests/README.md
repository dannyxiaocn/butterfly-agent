# `tests/`

Automated coverage for the runtime, providers, CLI, entities, and tool system.

## What This Part Is

- root-level tests: cross-cutting behavior such as agent execution, CLI commands, IPC, providers, session capabilities, and entity loading
- `runtime/`: focused tests for session creation, meta sessions, gene/bootstrap behavior, and watcher integration
- `tool_engine/`: reserved place for tool-engine-specific tests

## How To Use It

```bash
pytest tests/ -q
pytest tests/runtime/ -q
```

Target a single test module when changing one subsystem.

## How It Contributes To The Whole System

The runtime relies heavily on filesystem contracts and integration between layers. This directory is the main guard against regressions in those contracts.

- [tests/runtime/README.md](/Users/xiaobocheng/agent_core/nutshell/tests/runtime/README.md)
- [tests/tool_engine/README.md](/Users/xiaobocheng/agent_core/nutshell/tests/tool_engine/README.md)

