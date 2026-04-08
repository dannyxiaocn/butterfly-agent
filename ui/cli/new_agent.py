"""nutshell-new-agent: scaffold a new agent entity directory.

Usage (interactive — recommended):
    nutshell-new-agent

Usage (non-interactive / scripted):
    nutshell-new-agent -n my-agent
    nutshell-new-agent -n my-agent --extends kimi_core
    nutshell-new-agent -n my-agent --standalone
    nutshell-new-agent -n my-agent --entity-dir path/to/entity

When run without --extends / --standalone, prompts interactively for the
parent entity. Available entities are auto-detected from the entity directory.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path


# ── YAML templates ────────────────────────────────────────────────────────────

_AGENT_YAML_INHERITING = """\
name: {name}
description: ""
extends: {parent}
release_policy: persistent
max_iterations: 20

prompts:
  system:           # inherited from {parent}
  heartbeat:        # inherited from {parent}
  session_context:   # inherited from {parent}

tools:    # inherited from {parent}

skills:   # inherited from {parent}
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
  session_context: prompts/session.md

tools:
  - tools/bash.json
  - tools/web_search.json

skills: []
"""


# ── Entity detection ──────────────────────────────────────────────────────────

def _list_entities(entity_dir: Path) -> list[str]:
    """Return sorted list of entity names (dirs with agent.yaml) in entity_dir."""
    if not entity_dir.is_dir():
        return []
    return sorted(
        d.name for d in entity_dir.iterdir()
        if d.is_dir() and (d / "agent.yaml").exists()
    )


# ── Interactive prompts ───────────────────────────────────────────────────────

def _ask_name() -> str:
    while True:
        name = input("Agent name: ").strip()
        if name:
            return name
        print("  Name cannot be empty.")


def _ask_parent(entity_dir: Path) -> str | None:
    """Show numbered entity list, return selected entity name or None (standalone)."""
    entities = _list_entities(entity_dir)
    default_idx = next((i for i, n in enumerate(entities, 1) if n == "agent"), 1)

    print("\nExtend which entity?")
    for i, name in enumerate(entities, 1):
        suffix = "  (default)" if i == default_idx else ""
        print(f"  {i}. {name}{suffix}")
    standalone_idx = len(entities) + 1
    print(f"  {standalone_idx}. Standalone (no inheritance)")

    while True:
        raw = input(f"\nChoice [{default_idx}]: ").strip()
        if not raw:
            return entities[default_idx - 1] if entities else None
        try:
            n = int(raw)
            if 1 <= n <= len(entities):
                return entities[n - 1]
            if n == standalone_idx:
                return None
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {standalone_idx}.")


# ── File scaffolding ──────────────────────────────────────────────────────────

def _read_template(template_name: str, entity_dir: Path) -> str | None:
    """Try to read a file from entity/agent/. Returns None if not found."""
    candidate = entity_dir / "agent" / template_name
    if candidate.exists():
        return candidate.read_text(encoding="utf-8")
    return None


def create_entity(name: str, base_dir: Path, parent: str | None) -> Path:
    entity_dir = base_dir / name
    if entity_dir.exists():
        print(f"Error: entity '{name}' already exists at {entity_dir}", file=sys.stderr)
        sys.exit(1)

    if parent is not None and not (base_dir / parent / "agent.yaml").exists():
        raise ValueError(f"Parent entity '{parent}' not found in {base_dir}")

    (entity_dir / "prompts").mkdir(parents=True)
    (entity_dir / "skills").mkdir()
    (entity_dir / "tools").mkdir()

    if parent is not None:
        (entity_dir / "agent.yaml").write_text(
            _AGENT_YAML_INHERITING.format(name=name, parent=parent),
            encoding="utf-8",
        )
        # Empty placeholder files so the dirs show intent clearly
        (entity_dir / "skills" / ".gitkeep").write_text(
            f"# Add skill directories here and list them under `skills:` in agent.yaml.\n",
            encoding="utf-8",
        )
        (entity_dir / "tools" / ".gitkeep").write_text(
            f"# Add tool JSON files here and list them under `tools:` in agent.yaml.\n",
            encoding="utf-8",
        )
        (entity_dir / "prompts" / ".gitkeep").write_text(
            f"# Add prompt .md files here and set their paths under `prompts:` in agent.yaml.\n",
            encoding="utf-8",
        )
    else:
        (entity_dir / "agent.yaml").write_text(
            _AGENT_YAML_STANDALONE.format(name=name),
            encoding="utf-8",
        )
        system_md = _read_template("prompts/system.md", base_dir) or "You are a helpful, precise assistant.\n"
        heartbeat_md = _read_template("prompts/heartbeat.md", base_dir) or (
            "Heartbeat activation.\n\nCurrent tasks:\n{tasks}\n\n"
            "Pick up where you left off.\n\n"
            "If all tasks are done, clear the board via bash then respond: SESSION_FINISHED\n"
        )
        session_context_md = _read_template("prompts/session.md", base_dir) or (
            "## Session Files\n\nYour session directory: `sessions/{session_id}/`\n\n"
            "- `core/params.json` — model, provider, heartbeat_interval\n"
            "- `core/tasks/` — task cards (each .md file is a task with YAML frontmatter)\n"
            "- `core/memory.md` — persistent memory\n"
            "- `core/skills/` — session-level skills\n"
            "- `core/tools/` — session-level tools\n"
        )
        (entity_dir / "prompts" / "system.md").write_text(system_md, encoding="utf-8")
        (entity_dir / "prompts" / "heartbeat.md").write_text(heartbeat_md, encoding="utf-8")
        (entity_dir / "prompts" / "session.md").write_text(session_context_md, encoding="utf-8")

        for tool_file in ["tools/bash.json", "tools/web_search.json"]:
            content = _read_template(tool_file, base_dir)
            if content is not None:
                (entity_dir / tool_file).write_text(content, encoding="utf-8")

    return entity_dir
