"""butterfly-new-agent: scaffold a new agent entity directory.

Usage (interactive — recommended):
    butterfly-new-agent

Usage (non-interactive / scripted):
    butterfly-new-agent -n my-agent
    butterfly-new-agent -n my-agent --init-from agent
    butterfly-new-agent -n my-agent --entity-dir path/to/entity

When run without --init-from, creates a blank entity with empty prompt files.
With --init-from <source>, copies all files from the source entity and sets
the new entity's name in config.yaml. The copied entity is fully self-contained
and can be modified freely — there is no live inheritance link.
"""
from __future__ import annotations
import shutil
import sys
from pathlib import Path


# ── YAML template ─────────────────────────────────────────────────────────────

_CONFIG_YAML_EMPTY = """\
name: {name}
description: ""
model: claude-sonnet-4-6
provider: anthropic
max_iterations: 1000
thinking: false
thinking_budget: 8000
thinking_effort: high
tool_providers:
  web_search: brave
prompts:
  system: prompts/system.md
  task: prompts/task.md
  env: prompts/env.md
tools: []
skills: []
"""


# ── Entity detection ──────────────────────────────────────────────────────────

def _list_entities(entity_dir: Path) -> list[str]:
    """Return sorted list of entity names (dirs with config.yaml) in entity_dir."""
    if not entity_dir.is_dir():
        return []
    return sorted(
        d.name for d in entity_dir.iterdir()
        if d.is_dir() and (d / "config.yaml").exists()
    )


# ── Interactive prompts ───────────────────────────────────────────────────────

def _ask_name() -> str:
    while True:
        name = input("Agent name: ").strip()
        if name:
            return name
        print("  Name cannot be empty.")


def _ask_init_from(entity_dir: Path) -> str | None:
    """Show numbered entity list, return selected entity name or None (blank)."""
    entities = _list_entities(entity_dir)
    default_idx = next((i for i, n in enumerate(entities, 1) if n == "agent"), 1)

    print("\nInitialize from which entity?")
    for i, name in enumerate(entities, 1):
        suffix = "  (default)" if i == default_idx else ""
        print(f"  {i}. {name}{suffix}")
    blank_idx = len(entities) + 1
    print(f"  {blank_idx}. Blank (empty entity)")

    while True:
        raw = input(f"\nChoice [{default_idx}]: ").strip()
        if not raw:
            return entities[default_idx - 1] if entities else None
        try:
            n = int(raw)
            if 1 <= n <= len(entities):
                return entities[n - 1]
            if n == blank_idx:
                return None
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {blank_idx}.")


# ── File scaffolding ──────────────────────────────────────────────────────────

def _find_config_path(entity_dir: Path) -> Path | None:
    """Find config.yaml in an entity directory."""
    p = entity_dir / "config.yaml"
    return p if p.exists() else None


def create_entity(name: str, base_dir: Path, init_from: str | None) -> Path:
    """Create a new entity directory.

    If init_from is given, copies all files from that entity and updates the
    name in config.yaml. Otherwise, creates a blank entity with empty prompt
    files and a minimal config.yaml.

    Returns the path to the created entity directory.
    """
    entity_dir = base_dir / name
    if entity_dir.exists():
        print(f"Error: entity '{name}' already exists at {entity_dir}", file=sys.stderr)
        sys.exit(1)

    if init_from is not None:
        src_dir = base_dir / init_from
        if _find_config_path(src_dir) is None:
            raise ValueError(f"Source entity '{init_from}' not found in {base_dir}")

        # Copy entire source entity tree
        shutil.copytree(src_dir, entity_dir)

        # Update config: set new name and record init_from
        yaml_path = entity_dir / "config.yaml"

        import yaml as _yaml
        manifest = _yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        manifest["name"] = name
        manifest["init_from"] = init_from
        for field in ("extends", "link", "own", "append", "version", "meta_session"):
            manifest.pop(field, None)
        yaml_path.write_text(
            _yaml.dump(manifest, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    else:
        # Blank entity
        (entity_dir / "prompts").mkdir(parents=True)
        (entity_dir / "skills").mkdir()
        (entity_dir / "tools").mkdir()

        (entity_dir / "config.yaml").write_text(
            _CONFIG_YAML_EMPTY.format(name=name),
            encoding="utf-8",
        )
        (entity_dir / "prompts" / "system.md").write_text("", encoding="utf-8")
        (entity_dir / "prompts" / "task.md").write_text("", encoding="utf-8")
        (entity_dir / "prompts" / "env.md").write_text("", encoding="utf-8")
        (entity_dir / "tool.md").write_text("bash\nweb_search\nskill\nmanage_task\nrecall_memory\n", encoding="utf-8")

    return entity_dir
