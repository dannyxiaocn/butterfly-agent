# `tests/`

Automated coverage for the runtime, providers, CLI, entities, and tool system.

## What This Part Is

- `porter_system/`: the single home for all committed pytest modules in this repo
- `runtime/`: documentation-only area describing runtime coverage grouping
- `tool_engine/`: documentation-only area describing tool-engine coverage grouping

## How To Use It

```bash
pytest tests/ -q
pytest tests/porter_system -q
pytest tests/porter_system/test_session_engine_v1_3_77_* -q
pytest tests/porter_system/test_runtime_v1_3_77_* -q
```

Target a single porter-managed component glob when changing one subsystem.

## How It Contributes To The Whole System

The porter-managed suite keeps subsystem, integration, and layout coverage in one place so the repository has a single canonical pytest surface.

- [tests/runtime/README.md](/Users/xiaobocheng/agent_core/nutshell/tests/runtime/README.md)
- [tests/porter_system/README.md](/Users/xiaobocheng/agent_core/nutshell/tests/porter_system/README.md)
- [tests/tool_engine/README.md](/Users/xiaobocheng/agent_core/nutshell/tests/tool_engine/README.md)
