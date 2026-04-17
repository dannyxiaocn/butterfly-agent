"""butterfly-new-agent: scaffold a new agent directory.

Usage (interactive — recommended):
    butterfly-new-agent

Usage (non-interactive / scripted):
    butterfly-new-agent -n my-agent
    butterfly-new-agent -n my-agent --init-from agent
    butterfly-new-agent -n my-agent --agent-dir path/to/agenthub

When run without --init-from, creates a blank agent with empty prompt files.
With --init-from <source>, copies all files from the source agent and sets
the new agent's name in config.yaml. The copied agent is fully self-contained
and can be modified freely — there is no live inheritance link.
"""
from __future__ import annotations
import shutil
import sys
from pathlib import Path


# ── YAML template ─────────────────────────────────────────────────────────────

_CONFIG_YAML_EMPTY = """\
agent: {name}
description: ""
model: claude-sonnet-4-6
provider: anthropic
max_iterations: 1000
thinking: false
thinking_budget: 8000
thinking_effort: high
prompts:
  system: prompts/system.md
  task: prompts/task.md
  env: prompts/env.md
tools: []
skills: []
"""


# ── Agent detection ──────────────────────────────────────────────────────────

def _list_entities(agent_dir: Path) -> list[str]:
    """Return sorted list of agent names (dirs with config.yaml) in agent_dir."""
    if not agent_dir.is_dir():
        return []
    return sorted(
        d.name for d in agent_dir.iterdir()
        if d.is_dir() and (d / "config.yaml").exists()
    )


# ── Interactive prompts ───────────────────────────────────────────────────────

def _ask_name() -> str:
    while True:
        name = input("Agent name: ").strip()
        if name:
            return name
        print("  Name cannot be empty.")


def _ask_init_from(agent_dir: Path) -> str | None:
    """Show numbered agent list, return selected agent name or None (blank)."""
    agents = _list_entities(agent_dir)
    default_idx = next((i for i, n in enumerate(agents, 1) if n == "agent"), 1)

    print("\nInitialize from which agent?")
    for i, name in enumerate(agents, 1):
        suffix = "  (default)" if i == default_idx else ""
        print(f"  {i}. {name}{suffix}")
    blank_idx = len(agents) + 1
    print(f"  {blank_idx}. Blank (empty agent)")

    while True:
        raw = input(f"\nChoice [{default_idx}]: ").strip()
        if not raw:
            return agents[default_idx - 1] if agents else None
        try:
            n = int(raw)
            if 1 <= n <= len(agents):
                return agents[n - 1]
            if n == blank_idx:
                return None
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {blank_idx}.")


# ── File scaffolding ──────────────────────────────────────────────────────────

def _find_config_path(agent_dir: Path) -> Path | None:
    """Find config.yaml in an agent directory."""
    p = agent_dir / "config.yaml"
    return p if p.exists() else None


def create_agent(name: str, base_dir: Path, init_from: str | None) -> Path:
    """Create a new agent directory.

    If init_from is given, copies all files from that agent and updates the
    name in config.yaml. Otherwise, creates a blank agent with empty prompt
    files and a minimal config.yaml.

    Returns the path to the created agent directory.
    """
    agent_dir = base_dir / name
    if agent_dir.exists():
        print(f"Error: agent '{name}' already exists at {agent_dir}", file=sys.stderr)
        sys.exit(1)

    if init_from is not None:
        src_dir = base_dir / init_from
        if _find_config_path(src_dir) is None:
            raise ValueError(f"Source agent '{init_from}' not found in {base_dir}")

        # Copy entire source agent tree
        shutil.copytree(src_dir, agent_dir)

        # Update config: set new name and record init_from
        yaml_path = agent_dir / "config.yaml"

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
        # Blank agent
        (agent_dir / "prompts").mkdir(parents=True)
        (agent_dir / "skills").mkdir()
        (agent_dir / "tools").mkdir()

        (agent_dir / "config.yaml").write_text(
            _CONFIG_YAML_EMPTY.format(name=name),
            encoding="utf-8",
        )
        (agent_dir / "prompts" / "system.md").write_text("", encoding="utf-8")
        (agent_dir / "prompts" / "task.md").write_text("", encoding="utf-8")
        (agent_dir / "prompts" / "env.md").write_text("", encoding="utf-8")
        (agent_dir / "tools.md").write_text("bash\nweb_search\nskill\nmemory_recall\nmemory_update\n", encoding="utf-8")

    return agent_dir
