"""nutshell-new-agent: scaffold a new agent entity directory.

Usage:
    nutshell-new-agent -n my-agent
    nutshell-new-agent -n my-agent --entity-dir path/to/entity

Template files are copied from entity/agent_core/ if present in the current
working directory, so the scaffolded agent always uses up-to-date defaults.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

_AGENT_YAML = """\
name: {name}
description: ""
model: claude-sonnet-4-6
provider: anthropic
release_policy: persistent
max_iterations: 20

prompts:
  system: prompts/system.md
  heartbeat: prompts/heartbeat.md
  session_context: prompts/session_context.md

tools:
  - tools/bash.json

skills: []
"""

_BASH_JSON = json.dumps({
    "name": "bash",
    "description": (
        "Execute a shell command. Returns stdout+stderr combined and exit code. "
        "Use pty=true for commands that need an interactive terminal (color output, progress bars)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to execute."},
            "timeout": {"type": "number", "description": "Timeout in seconds. Omit to use the default (30s)."},
            "workdir": {"type": "string", "description": "Working directory path. Omit to use the current directory."},
            "pty": {
                "type": "boolean",
                "description": "Run in a pseudo-terminal. Preserves color output and isatty(). Unix only. Default false.",
            },
        },
        "required": ["command"],
    },
}, indent=2, ensure_ascii=False)


def _read_template(template_name: str, entity_dir: Path) -> str | None:
    """Try to read a template file from entity/agent_core/. Returns None if not found."""
    candidate = entity_dir / "agent_core" / template_name
    if candidate.exists():
        return candidate.read_text(encoding="utf-8")
    return None


def create_entity(name: str, base_dir: Path) -> Path:
    entity_dir = base_dir / name
    if entity_dir.exists():
        print(f"Error: entity '{name}' already exists at {entity_dir}", file=sys.stderr)
        sys.exit(1)

    (entity_dir / "prompts").mkdir(parents=True)
    (entity_dir / "skills").mkdir()
    (entity_dir / "tools").mkdir()

    (entity_dir / "agent.yaml").write_text(_AGENT_YAML.format(name=name), encoding="utf-8")

    # Copy prompt files from agent_core (canonical source), fallback to minimal defaults
    system_md = _read_template("prompts/system.md", base_dir)
    if system_md is None:
        system_md = "You are a helpful, precise assistant.\n"
    (entity_dir / "prompts" / "system.md").write_text(system_md, encoding="utf-8")

    heartbeat_md = _read_template("prompts/heartbeat.md", base_dir)
    if heartbeat_md is None:
        heartbeat_md = (
            "Heartbeat activation.\n\nCurrent tasks:\n{tasks}\n\n"
            "Pick up where you left off.\n\n"
            "If all tasks are done, clear the board via bash then respond: SESSION_FINISHED\n"
        )
    (entity_dir / "prompts" / "heartbeat.md").write_text(heartbeat_md, encoding="utf-8")

    session_context_md = _read_template("prompts/session_context.md", base_dir)
    if session_context_md is None:
        session_context_md = (
            "## Session Files\n\n"
            "Your session directory: `sessions/{session_id}/`\n\n"
            "- `params.json` — model, provider, heartbeat_interval\n"
            "- `tasks.md` — task board\n"
            "- `prompts/memory.md` — persistent memory\n"
            "- `skills/` — session-level skills\n"
        )
    (entity_dir / "prompts" / "session_context.md").write_text(session_context_md, encoding="utf-8")

    # Copy bash.json from agent_core, fallback to embedded default
    bash_json = _read_template("tools/bash.json", base_dir)
    if bash_json is None:
        bash_json = _BASH_JSON
    (entity_dir / "tools" / "bash.json").write_text(bash_json, encoding="utf-8")

    return entity_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nutshell-new-agent",
        description="Scaffold a new agent entity directory.",
    )
    parser.add_argument("-n", "--name", required=True, help="Entity name (e.g. my-agent)")
    parser.add_argument(
        "--entity-dir",
        default="entity",
        metavar="DIR",
        help="Base directory for entities (default: entity/)",
    )
    args = parser.parse_args()

    entity_dir = create_entity(args.name, Path(args.entity_dir))
    print(f"Created: {entity_dir}/")
    print(f"  agent.yaml")
    print(f"  prompts/system.md")
    print(f"  prompts/heartbeat.md")
    print(f"  prompts/session_context.md")
    print(f"  skills/")
    print(f"  tools/bash.json")


if __name__ == "__main__":
    main()
