"""nutshell-new-agent: scaffold a new agent entity directory.

Usage:
    nutshell-new-agent -n my-agent
    nutshell-new-agent -n my-agent --extends kimi_core
    nutshell-new-agent -n my-agent --no-inherit
    nutshell-new-agent -n my-agent --entity-dir path/to/entity

By default creates a minimal entity that extends agent_core. agent.yaml always
contains the full set of fields. Inheritance works at the file level: add a
file to prompts/, tools/, or skills/ to override; leave the directory empty to
inherit from the parent entity.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

_AGENT_YAML_INHERITING = """\
name: {name}
description: ""
model: {model}
provider: {provider}
extends: {parent}
release_policy: persistent
max_iterations: 20

prompts:
  system:           # inherited from {parent}
  heartbeat:        # inherited from {parent}
  session_context:  # inherited from {parent}

tools:    # inherited from {parent}

skills:   # inherited from {parent}
"""

_PLACEHOLDER_PROMPT = """\
# Inherited from {parent} — this file is not loaded until you set
# `{key}: prompts/{key}.md` in agent.yaml.
# Edit this file and update agent.yaml to override the parent prompt.
"""

_PLACEHOLDER_DIR = """\
# Inherited from {parent}.
# Add files here and list them under `{section}:` in agent.yaml to override.
"""

_AGENT_YAML_STANDALONE = """\
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
  - tools/web_search.json

skills: []
"""


def _read_agent_yaml(entity_name: str, base_dir: Path) -> dict | None:
    """Read and parse agent.yaml from a sibling entity to extract model/provider."""
    try:
        import yaml
        candidate = base_dir / entity_name / "agent.yaml"
        if candidate.exists():
            return yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return None


def _read_template(template_name: str, entity_dir: Path) -> str | None:
    """Try to read a file from entity/agent_core/. Returns None if not found."""
    candidate = entity_dir / "agent_core" / template_name
    if candidate.exists():
        return candidate.read_text(encoding="utf-8")
    return None


def create_entity(name: str, base_dir: Path, parent: str | None) -> Path:
    entity_dir = base_dir / name
    if entity_dir.exists():
        print(f"Error: entity '{name}' already exists at {entity_dir}", file=sys.stderr)
        sys.exit(1)

    (entity_dir / "prompts").mkdir(parents=True)
    (entity_dir / "skills").mkdir()
    (entity_dir / "tools").mkdir()

    if parent is not None:
        # Inherit model/provider from parent if available, fallback to agent_core defaults
        parent_yaml = _read_agent_yaml(parent, base_dir) or {}
        model = parent_yaml.get("model", "claude-sonnet-4-6")
        provider = parent_yaml.get("provider", "anthropic")
        yaml_content = _AGENT_YAML_INHERITING.format(
            name=name, parent=parent, model=model, provider=provider
        )
        (entity_dir / "agent.yaml").write_text(yaml_content, encoding="utf-8")

        # Placeholder prompt files — not loaded until yaml value is set
        for key in ("system", "heartbeat", "session_context"):
            content = _PLACEHOLDER_PROMPT.format(parent=parent, key=key)
            (entity_dir / "prompts" / f"{key}.md").write_text(content, encoding="utf-8")

        # Placeholder .gitkeep for tools/ and skills/
        for section in ("tools", "skills"):
            content = _PLACEHOLDER_DIR.format(parent=parent, section=section)
            (entity_dir / section / ".gitkeep").write_text(content, encoding="utf-8")
    else:
        yaml_content = _AGENT_YAML_STANDALONE.format(name=name)
        (entity_dir / "agent.yaml").write_text(yaml_content, encoding="utf-8")

        # Copy files from agent_core as starting point
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

        for tool_file in ["tools/bash.json", "tools/web_search.json"]:
            content = _read_template(tool_file, base_dir)
            if content is not None:
                (entity_dir / tool_file).write_text(content, encoding="utf-8")

    return entity_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nutshell-new-agent",
        description="Scaffold a new agent entity directory.",
    )
    parser.add_argument("-n", "--name", required=True, help="Entity name (e.g. my-agent)")
    parser.add_argument(
        "--extends",
        default="agent_core",
        metavar="PARENT",
        help="Parent entity to inherit files from (default: agent_core).",
    )
    parser.add_argument(
        "--no-inherit",
        action="store_true",
        help="Create a fully standalone agent (copies files from agent_core, no extends).",
    )
    parser.add_argument(
        "--entity-dir",
        default="entity",
        metavar="DIR",
        help="Base directory for entities (default: entity/)",
    )
    args = parser.parse_args()

    parent = None if args.no_inherit else args.extends
    entity_dir = create_entity(args.name, Path(args.entity_dir), parent)

    print(f"Created: {entity_dir}/")
    print(f"  agent.yaml")
    if parent:
        print(f"  prompts/   (empty — inherits from '{parent}')")
        print(f"  tools/     (empty — inherits from '{parent}')")
        print(f"  skills/    (empty — inherits from '{parent}')")
        print(f"  Add files to override specific items from the parent.")
    else:
        print(f"  prompts/system.md")
        print(f"  prompts/heartbeat.md")
        print(f"  prompts/session_context.md")
        print(f"  skills/")
        print(f"  tools/bash.json")
        print(f"  tools/web_search.json")


if __name__ == "__main__":
    main()
