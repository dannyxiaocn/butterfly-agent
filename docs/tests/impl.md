# Tests — Implementation

## Structure

Tests mirror the source code layout:

- `nutshell/core/` — Agent, Tool, Skill unit tests
- `nutshell/llm_engine/` — provider streaming, registry, prompt caching
- `nutshell/runtime/` — IPC, CAP, watcher, meta-session, session factory
- `nutshell/service/` — history service adapter tests
- `nutshell/session_engine/` — session lifecycle, task cards, venv, params
- `nutshell/skill_engine/` — skill loader, frontmatter parsing, renderer
- `nutshell/tool_engine/` — tool loader, executors, bash/skill/reload tools
- `entity/` — entity manifest contracts, docs existence
- `ui/cli/` — CLI command tests
- `ui/web/` — web app and helper tests
- `integration/` — cross-component end-to-end tests

## Usage

```bash
pytest tests/ -q                                    # All tests
pytest tests/nutshell/session_engine/ -q             # One subsystem
pytest tests/nutshell/core/test_agent.py -q          # One file
pytest tests/ui/cli/ -q                              # CLI tests only
```

## Configuration

`pyproject.toml`:
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```
