# `nutshell/`

This package is the implementation of the runtime itself. The repo root holds product-level docs and assets; this directory holds the Python code that makes sessions run.

## Subsystems

- `core/`: agent loop and shared abstractions.
- `llm_engine/`: provider adapters and registry.
- `tool_engine/`: tool loading, executors, built-ins, and hot reload.
- `skill_engine/`: skill loading and rendering.
- `session_engine/`: entity loading, meta-session handling, session creation, and session runtime wrapper.
- `runtime/`: watcher, file IPC, frontend bridge, and coordination primitives.

## How To Use This Part

Most users do not import `nutshell/` piecemeal. They interact through:

- the CLI in `ui/cli/`
- the Web UI in `ui/web/`
- the public package exports in [nutshell/__init__.py](/Users/xiaobocheng/agent_core/nutshell/nutshell/__init__.py)

Use this directory directly when extending the runtime itself.

## How It Contributes To The Whole System

This is the engine layer beneath entities, sessions, and UI. Everything above it is configuration, orchestration, or presentation.

