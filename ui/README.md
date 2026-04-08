# `ui/`

This directory exposes the runtime to humans and external channels. It is intentionally thin: it translates user actions into file operations and runtime calls.

## Subdirectories

- `cli/`: the `nutshell` command-line interface.
- `web/`: FastAPI, SSE streaming, and the optional WeChat bridge.

## How To Use It

```bash
nutshell chat "hello"
nutshell sessions
nutshell web
```

## How It Contributes To The Whole System

The runtime can run without this directory, but this is how operators actually create sessions, inspect them, and send messages.

- [cli/README.md](/Users/xiaobocheng/agent_core/nutshell/ui/cli/README.md)
- [web/README.md](/Users/xiaobocheng/agent_core/nutshell/ui/web/README.md)

