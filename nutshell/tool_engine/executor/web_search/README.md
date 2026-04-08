# nutshell/tool_engine/executor/web_search

This directory contains search backends for the built-in `web_search` tool.

## What It Is

- `brave_web_search.py`: default Brave Search implementation
- `tavily_web_search.py`: alternate Tavily implementation

## How To Use It

Set `tool_providers.web_search` in `sessions/<id>/core/params.json` to choose the backend:

```json
{"tool_providers": {"web_search": "tavily"}}
```

## How It Fits

The rest of the runtime sees one logical `web_search` tool. This directory provides the swappable backend implementations behind that single tool name.
