"""Agent configuration — YAML-based agent manifest.

AgentConfig reads config.yaml files from disk and provides a typed view
over agent manifests. Moved from core/ because from_path() does file IO
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
        config_path = path if path.name == "config.yaml" else path / "config.yaml"
        if not config_path.exists():
            raise FileNotFoundError(f"config.yaml not found: {config_path}")

        manifest = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return cls(path=config_path.parent, manifest=manifest)
