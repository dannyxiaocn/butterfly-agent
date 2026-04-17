# Tests — Implementation

## Structure

Tests mirror the source code layout:

- `butterfly/core/` — Agent, Tool, Skill unit tests
- `butterfly/llm_engine/` — provider streaming, registry, prompt caching
- `butterfly/runtime/` — IPC, CAP, watcher, meta-session, session factory
- `butterfly/service/` — history service adapter tests
- `butterfly/session_engine/` — session lifecycle, task cards, venv, params
- `butterfly/skill_engine/` — skill loader, frontmatter parsing, renderer
- `butterfly/tool_engine/` — tool loader, executors, bash/skill/reload tools
- `agenthub/` — agent manifest contracts, docs existence
- `ui/cli/` — CLI command tests
- `ui/web/` — web app and helper tests
- `integration/` — cross-component end-to-end tests

## Usage

```bash
pytest tests/ -q                                    # All tests
pytest tests/butterfly/session_engine/ -q             # One subsystem
pytest tests/butterfly/core/test_agent.py -q          # One file
pytest tests/ui/cli/ -q                              # CLI tests only
```

## Configuration

`pyproject.toml`:
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```
