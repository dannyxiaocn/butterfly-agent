# Butterfly🦋Agent

Agent system that serves all humans.

## Quick Start

```bash
pip install -e .

# One-command login for the default provider (ChatGPT OAuth for Codex).
# Runs `codex login` under the hood and verifies ~/.codex/auth.json.
butterfly codex login

# Or, if you prefer Kimi For Coding (Moonshot):
#   prompts for your API key, writes it to .env (chmod 0600), and validates.
butterfly kimi login

butterfly                       # start server + web UI; print URL; hang
butterfly chat "hello"          # auto-starts server if needed
butterfly server                # tail the running server's log
butterfly update                # git pull + pip install + rebuild + restart
```

Both login commands are idempotent and print step-by-step tutorials if any
dependency is missing (e.g. the `codex` CLI isn't on PATH yet).

## Using & Developing

One skill carries the full guide — load it inside Claude Code / Butterfly when you need it:

- **`butterfly`** — unified guide covering CLI usage (run agents, manage sessions, create agents) and codebase development (runtime, providers, tool/skill engine, etc.)

## Documentation

Everything else lives in [`docs/`](docs/), mirroring the source tree. Each component directory has three files:

| File | Purpose |
|------|---------|
| `design.md` | Architecture and rationale |
| `impl.md` | Implementation reference — files, APIs, behaviors |
| `todo.md` | Work log, known bugs, future directions |

Start here:

- [`docs/butterfly/design.md`](docs/butterfly/design.md) — runtime architecture
- [`docs/agent/design.md`](docs/agent/design.md) — agent template system
- [`docs/ui/design.md`](docs/ui/design.md) — CLI and Web frontends
