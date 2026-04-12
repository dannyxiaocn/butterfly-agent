"""Agent configuration — YAML-based entity manifest.

AgentConfig reads agent.yaml files from disk and provides a typed view
over entity manifests. Moved from core/ because from_path() does file IO
and belongs in the session engine, not the core pure-computation layer.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any  # used in manifest type annotation


@dataclass(frozen=True)
class AgentConfig:
    path: Path
    manifest: dict[str, Any]

    @property
    def init_from(self) -> str | None:
        """Name of the entity this was initialized from (documentation only)."""
        value = self.manifest.get("init_from")
        return str(value) if value else None

    @classmethod
    def from_path(cls, path: Path) -> "AgentConfig":
        try:
            import yaml
        except ImportError as exc:
            raise ImportError("Install pyyaml to use AgentConfig: pip install pyyaml") from exc

        path = Path(path)
        manifest_path = path if path.name == "agent.yaml" else path / "agent.yaml"
        if not manifest_path.exists():
            raise FileNotFoundError(f"agent.yaml not found: {manifest_path}")

        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        return cls(path=manifest_path.parent, manifest=manifest)
