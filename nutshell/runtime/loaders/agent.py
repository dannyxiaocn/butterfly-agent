from __future__ import annotations
from pathlib import Path
from typing import Callable

from nutshell.abstract.loader import BaseLoader
from nutshell.core.agent import Agent
from nutshell.runtime.loaders.skill import SkillLoader
from nutshell.runtime.loaders.tool import ToolLoader


def _load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


class AgentLoader(BaseLoader[Agent]):
    """Load a complete Agent from an entity directory containing agent.yaml.

    Reads the manifest (agent.yaml) and uses SkillLoader and ToolLoader to
    assemble a fully configured Agent instance.

    Args:
        impl_registry: Optional dict mapping tool name -> callable, passed
                       to ToolLoader so tool implementations are wired up.

    Example::

        agent = AgentLoader(impl_registry={"echo": lambda text: text}).load(
            Path("entity/core_agent")
        )
    """

    def __init__(self, impl_registry: dict[str, Callable] | None = None) -> None:
        self._impl_registry = impl_registry or {}

    def load(self, path: Path) -> Agent:
        """Load agent from a directory containing agent.yaml."""
        try:
            import yaml
        except ImportError:
            raise ImportError("Install pyyaml to use AgentLoader: pip install pyyaml")

        path = Path(path)
        manifest_path = path / "agent.yaml"
        if not manifest_path.exists():
            raise FileNotFoundError(f"agent.yaml not found in: {path}")

        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        prompts_cfg = manifest.get("prompts", {}) or {}

        # Load prompts
        system_prompt = ""
        if "system" in prompts_cfg:
            system_prompt = _load_prompt(path / prompts_cfg["system"])

        heartbeat_prompt = ""
        if "heartbeat" in prompts_cfg:
            heartbeat_prompt = _load_prompt(path / prompts_cfg["heartbeat"])

        # Load skills
        skills_cfg = manifest.get("skills", []) or []
        skills = [SkillLoader().load(path / s) for s in skills_cfg]

        # Load tools
        tools_cfg = manifest.get("tools", []) or []
        tool_loader = ToolLoader(impl_registry=self._impl_registry)
        tools = [tool_loader.load(path / t) for t in tools_cfg]

        return Agent(
            system_prompt=system_prompt,
            tools=tools,
            skills=skills,
            model=manifest.get("model", "claude-sonnet-4-6"),
            release_policy=manifest.get("release_policy", "persistent"),
            max_iterations=manifest.get("max_iterations", 20),
            heartbeat_prompt=heartbeat_prompt,
        )

    def load_dir(self, directory: Path) -> list[Agent]:
        """Load all agents from subdirectories that contain agent.yaml."""
        directory = Path(directory)
        agents = []
        for subdir in sorted(directory.iterdir()):
            if subdir.is_dir() and (subdir / "agent.yaml").exists():
                agents.append(self.load(subdir))
        return agents
