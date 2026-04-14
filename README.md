# Butterfly🦋Agent

Agent system that serves all humans.

## Quick Start

```bash
pip install -e .

codex login                     # default entity uses codex-oauth / gpt-5.4

butterfly-server                # auto-daemonizes
butterfly chat "hello"          # auto-starts server if needed
```

## Using & Developing

Two skills carry the full guides — load them inside Claude Code / Butterfly when you need them:

- **`use-butterfly`** — how to run the CLI, manage sessions, create entities
- **`dev-butterfly`** — how to work on the Butterfly codebase itself

## Documentation

Everything else lives in [`docs/`](docs/), mirroring the source tree. Each component directory has three files:

| File | Purpose |
|------|---------|
| `design.md` | Architecture and rationale |
| `impl.md` | Implementation reference — files, APIs, behaviors |
| `todo.md` | Work log, known bugs, future directions |

Start here:

- [`docs/butterfly/design.md`](docs/butterfly/design.md) — runtime architecture
- [`docs/entity/design.md`](docs/entity/design.md) — entity template system
- [`docs/ui/design.md`](docs/ui/design.md) — CLI and Web frontends
