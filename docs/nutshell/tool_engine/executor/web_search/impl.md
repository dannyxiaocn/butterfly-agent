# Web Search Executor — Implementation

## Files

| File | Purpose |
|------|---------|
| `brave_web_search.py` | Default Brave Search implementation |
| `tavily_web_search.py` | Alternate Tavily implementation |

## Backend Selection

Set in `sessions/<id>/core/config.yaml`:
```json
{"tool_providers": {"web_search": "tavily"}}
```

Default is `brave`.
