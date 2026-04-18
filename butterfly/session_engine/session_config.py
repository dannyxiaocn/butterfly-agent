"""Unified session/agent configuration — config.yaml reader/writer.

Replaces session_params.py. Both agenthub/ and session core/ use identical
config.yaml files. System runtime state (agent_version, pid, etc.) lives
in _sessions/<id>/status.json instead.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as _yaml_exc:  # pragma: no cover - surfaced on first call
    yaml = None  # type: ignore[assignment]
    _YAML_IMPORT_ERROR: ImportError | None = _yaml_exc
else:
    _YAML_IMPORT_ERROR = None


def _require_yaml() -> Any:
    if yaml is None:
        raise ImportError("Install pyyaml: pip install pyyaml") from _YAML_IMPORT_ERROR
    return yaml


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically.

    The YAML PUT endpoint is network-reachable (v2.0.9), so a concurrent
    read must never observe a half-written file. write_text() is racy; we
    write into the same directory (cross-fs os.replace would fail), fsync,
    then rename. On rename failure the tmp file is cleaned up.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # delete=False so we control cleanup; dir= keeps the rename on-fs.
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

# User-configurable defaults. Agent config.yaml and session core/config.yaml
# share the same schema — agent defines defaults, session inherits and can override.
DEFAULT_CONFIG: dict[str, Any] = {
    "agent": "",
    "description": "",
    "model": None,
    "provider": None,
    "fallback_model": None,
    "fallback_provider": None,
    "max_iterations": 1000,
    "thinking": False,
    "thinking_budget": 8000,
    "thinking_effort": "high",
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
    """Return path to config.yaml. Works for both agent dirs and session dirs.

    For agents: agenthub/<name>/config.yaml
    For sessions: sessions/<id>/core/config.yaml
    """
    core_dir = base_dir / "core"
    if core_dir.is_dir():
        return core_dir / "config.yaml"
    return base_dir / "config.yaml"


def read_config(base_dir: Path) -> dict:
    """Read config.yaml."""
    y = _require_yaml()
    path = config_path(base_dir)
    if path.exists():
        try:
            raw = y.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        # Legacy `name:` → `agent:` migration (v2.0.19 rename). Sessions
        # saved before the rename carry `name: <agent-name>`; surface it
        # under the new key so update_config_yaml's whitelist doesn't
        # silently drop the identifier on the next write.
        if "name" in raw and not raw.get("agent"):
            raw["agent"] = raw["name"]
        raw.pop("name", None)
        return {**DEFAULT_CONFIG, **raw}

    return dict(DEFAULT_CONFIG)


def write_config(base_dir: Path, **updates: Any) -> None:
    """Merge updates into config.yaml. Atomic via tempfile + os.replace."""
    y = _require_yaml()
    current = read_config(base_dir)
    current.update(updates)
    path = config_path(base_dir)
    _atomic_write_text(
        path,
        y.dump(current, default_flow_style=False, allow_unicode=True, sort_keys=False),
    )


def ensure_config(base_dir: Path, **defaults: Any) -> None:
    """Create config.yaml if absent."""
    path = config_path(base_dir)
    if path.exists():
        return
    y = _require_yaml()
    content = {**DEFAULT_CONFIG, **defaults}
    _atomic_write_text(
        path,
        y.dump(content, default_flow_style=False, allow_unicode=True, sort_keys=False),
    )
