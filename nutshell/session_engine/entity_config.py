"""Agent configuration — YAML-based entity manifest.

AgentConfig reads config.yaml files from disk and provides a typed view
over entity manifests. Moved from core/ because from_path() does file IO
and belongs in the session engine, not the core pure-computation layer.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentConfig:
    path: Path
    manifest: dict[str, Any]

    @classmethod
    def from_path(cls, path: Path) -> "AgentConfig":
        try:
            import yaml
        except ImportError as exc:
            raise ImportError("Install pyyaml to use AgentConfig: pip install pyyaml") from exc

        path = Path(path)
        # Support both config.yaml (new) and agent.yaml (legacy)
        config_path = path if path.name in ("config.yaml", "agent.yaml") else path / "config.yaml"
        if not config_path.exists():
            legacy_path = path / "agent.yaml"
            if legacy_path.exists():
                config_path = legacy_path
            else:
                raise FileNotFoundError(f"config.yaml not found: {config_path}")

        manifest = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return cls(path=config_path.parent, manifest=manifest)
