# `nutshell/tool_engine/executor`

Executors are the concrete runtimes behind tools.

## Subdirectories

- `terminal/`: shell-based execution. `bash_terminal.py` is the built-in shell tool; `shell_terminal.py` runs agent-authored `.sh` tools.
- `skill/`: the built-in `skill` tool, which loads a skill body and related files into context on demand.
- `web_search/`: Brave and Tavily implementations for the `web_search` tool.

## How To Use This Part

You usually reach these executors through `ToolLoader`, not by instantiating them directly. Edit this directory when changing how a tool category executes.

## How It Contributes To The Whole System

This directory is where abstract tool definitions become real behavior. It is the lowest layer of the tool system.

