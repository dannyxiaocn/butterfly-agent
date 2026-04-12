"""Unified session/entity configuration — config.yaml reader/writer.

Replaces session_params.py. Both entity/ and session core/ use identical
config.yaml files. System runtime state (agent_version, pid, etc.) lives
in _sessions/<id>/status.json instead.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# User-configurable defaults. Entity config.yaml and session core/config.yaml
# share the same schema — entity defines defaults, session inherits and can override.
DEFAULT_CONFIG: dict[str, Any] = {
    "name": "",
    "description": "",
    "model": None,
    "provider": None,
    "fallback_model": None,
    "fallback_provider": None,
    "max_iterations": 20,
    "thinking": False,
    "thinking_budget": 8000,
    "thinking_effort": "high",
    "tool_providers": {"web_search": "brave"},
    "prompts": {
        "system": "system.md",
        "task": "task.md",
        "env": "env.md",
    },
    "tools": [],
    "skills": [],
    "duty": None,  # Optional dict: {"interval": N, "description": "..."}
}


def config_path(base_dir: Path) -> Path:
    """Return path to config.yaml. Works for both entity dirs and session dirs.

    For entities: entity/<name>/config.yaml
    For sessions: sessions/<id>/core/config.yaml
    """
    core_dir = base_dir / "core"
    if core_dir.is_dir():
        return core_dir / "config.yaml"
    return base_dir / "config.yaml"


def read_config(base_dir: Path) -> dict:
    """Read config.yaml, falling back to legacy params.json for old sessions."""
    try:
        import yaml
    except ImportError as exc:
        raise ImportError("Install pyyaml: pip install pyyaml") from exc

    path = config_path(base_dir)
    if path.exists():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            raw = {}
        return {**DEFAULT_CONFIG, **raw}

    # Fallback: read legacy params.json for backward compatibility
    legacy_path = base_dir / "core" / "params.json"
    if not legacy_path.exists():
        legacy_path = base_dir / "params.json"  # direct entity layout
    if legacy_path.exists():
        import json
        try:
            raw = json.loads(legacy_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {**DEFAULT_CONFIG, **raw}
        except Exception:
            pass

    return dict(DEFAULT_CONFIG)


def write_config(base_dir: Path, **updates: Any) -> None:
    """Merge updates into config.yaml."""
    import yaml

    current = read_config(base_dir)
    current.update(updates)
    path = config_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(current, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def ensure_config(base_dir: Path, **defaults: Any) -> None:
    """Create config.yaml if absent."""
    path = config_path(base_dir)
    if path.exists():
        return
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    content = {**DEFAULT_CONFIG, **defaults}
    path.write_text(
        yaml.dump(content, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
