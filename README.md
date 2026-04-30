# Butterfly Agent 🦋

Think Wild

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
```

Both login commands are idempotent and print step-by-step tutorials if any
dependency is missing (e.g. the `codex` CLI isn't on PATH yet).

## Using & Developing

One skill carries the full guide — load it inside Claude Code / Butterfly when you need it:

- **`butterfly`** — unified guide covering CLI usage (run agents, manage sessions, create agents) and codebase development (runtime, providers, tool/skill engine, etc.)

## Documentation

Everything else lives in [`docs/`](docs/), mirroring the source tree. One design doc per module.

Start here:

- [`docs/butterfly/runtime/events.md`](docs/butterfly/runtime/events.md) — `events_v1.jsonl` contract (the source of truth)
- [`docs/butterfly/runtime/io.md`](docs/butterfly/runtime/io.md) — `butterfly.runtime.io` — the only read/write surface
- [`docs/butterfly/session_engine/design.md`](docs/butterfly/session_engine/design.md) — daemon run loop
- [`docs/butterfly/session_engine/agent_team.md`](docs/butterfly/session_engine/agent_team.md) — `kind: team` sessions + teamchat
- [`docs/butterfly/tool_engine/workflow.md`](docs/butterfly/tool_engine/workflow.md) — multi-step sub-agent pipeline tool
- [`docs/ui/web/design.md`](docs/ui/web/design.md) — HTTP + SSE + unified Card
- [`docs/ui/cli/design.md`](docs/ui/cli/design.md) — `butterfly io` reflection + friendly aliases
