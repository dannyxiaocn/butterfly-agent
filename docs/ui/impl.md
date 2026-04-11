# UI — Implementation

## Subdirectories

- `cli/`: the `nutshell` command-line interface
- `web/`: FastAPI, SSE streaming, optional WeChat bridge

## Usage

```bash
nutshell chat "hello"
nutshell sessions
nutshell web
```

The runtime can run without this directory, but this is how operators create sessions, inspect them, and send messages.
