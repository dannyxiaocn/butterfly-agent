# Tests — Design

Automated coverage for the nutshell runtime, providers, CLI, entities, and tool system. Tests mirror the source code directory structure for natural discoverability.

## Layout

```
tests/
  nutshell/              mirrors nutshell/ source tree
    core/                agent, tool, skill unit tests
    llm_engine/          provider and registry tests
    runtime/             IPC, CAP, watcher, meta-session tests
    service/             service layer tests
    session_engine/      session lifecycle, task cards, venv tests
    skill_engine/        skill loader and renderer tests
    tool_engine/         tool loader, executor, bash/skill tool tests
  entity/                entity manifest and docs contract tests
  ui/                    mirrors ui/ source tree
    cli/                 CLI command and helper tests
    web/                 web app and helper tests
  integration/           cross-component integration tests
```

Tests are discovered via standard pytest with `testpaths = ["tests"]`. No custom runner infrastructure is required.
