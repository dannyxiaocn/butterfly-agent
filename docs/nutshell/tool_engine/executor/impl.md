# Executors — Implementation

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `terminal/` | Shell execution: `bash_terminal.py` (built-in bash), `shell_terminal.py` (agent-authored .sh tools) |
| `skill/` | `skill_tool.py`: SkillExecutor, variable substitution, `create_skill_tool()` |
| `web_search/` | `brave_web_search.py`, `tavily_web_search.py` |

Executors are reached through `ToolLoader`, not instantiated directly.
