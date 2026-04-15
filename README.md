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

One skill carries the full guide — load it inside Claude Code / Butterfly when you need it:

- **`butterfly`** — unified guide covering CLI usage (run agents, manage sessions, create entities) and codebase development (runtime, providers, tool/skill engine, etc.)

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
