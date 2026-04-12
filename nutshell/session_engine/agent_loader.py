from __future__ import annotations
from pathlib import Path
from typing import Callable

from nutshell.core.loader import BaseLoader
from nutshell.session_engine.entity_config import AgentConfig
from nutshell.core.agent import Agent
from nutshell.skill_engine.loader import SkillLoader
from nutshell.tool_engine.executor.skill.skill_tool import create_skill_tool
from nutshell.tool_engine.loader import ToolLoader


def _load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


class AgentLoader(BaseLoader[Agent]):
    """Load a complete Agent from an entity directory containing config.yaml.

    Each entity is fully self-contained — all prompts, tools, skills, model,
    and provider are declared explicitly in config.yaml.
    """

    def __init__(self, impl_registry: dict[str, Callable] | None = None) -> None:
        self._impl_registry = impl_registry or {}

    def load(self, path: Path) -> Agent:
        """Load agent from a directory containing config.yaml."""
        path = Path(path)
        config = AgentConfig.from_path(path)
        manifest = config.manifest

        def resolve_file(rel: str) -> Path | None:
            p = path / rel
            return p if p.exists() else None

        child_prompts = manifest.get("prompts") or {}

        def load_prompt_key(key: str) -> str:
            rel = child_prompts.get(key)
            if rel:
                p = path / rel
                return _load_prompt(p) if p.exists() else ""
            return ""

        system_prompt            = load_prompt_key("system")
        heartbeat_prompt         = load_prompt_key("heartbeat")
        session_context_template = load_prompt_key("session_context")

        raw_skills = manifest.get("skills") or []
        skills = [
            SkillLoader().load(resolved)
            for s in raw_skills
            if (resolved := resolve_file(s)) is not None
        ]

        raw_tools = manifest.get("tools") or []
        tool_loader = ToolLoader(impl_registry=self._impl_registry, skills=skills)
        tools = [
            tool_loader.load(resolved)
            for t in raw_tools
            if (resolved := resolve_file(t)) is not None
        ]

        if any(t.name == "skill" for t in tools):
            tools = [create_skill_tool(skills) if t.name == "skill" else t for t in tools]

        model = manifest.get("model") or "claude-sonnet-4-6"
        provider_str = manifest.get("provider") or "anthropic"
        fallback_model = manifest.get("fallback_model") or ""
        fallback_provider = manifest.get("fallback_provider") or ""

        agent = Agent(
            system_prompt=system_prompt,
            tools=tools,
            skills=skills,
            model=model,
            max_iterations=manifest.get("max_iterations", 20),
            heartbeat_prompt=heartbeat_prompt,
            session_context_template=session_context_template,
            fallback_model=fallback_model,
            fallback_provider=fallback_provider,
        )

        try:
            from nutshell.llm_engine.registry import resolve_provider
            agent._provider = resolve_provider(provider_str)
        except Exception:
            pass

        return agent

    def load_dir(self, directory: Path) -> list[Agent]:
        """Load all agents from subdirectories that contain config.yaml."""
        directory = Path(directory)
        agents = []
        for subdir in sorted(directory.iterdir()):
            if subdir.is_dir() and ((subdir / "config.yaml").exists() or (subdir / "agent.yaml").exists()):
                agents.append(self.load(subdir))
        return agents
