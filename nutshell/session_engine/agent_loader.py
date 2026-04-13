from __future__ import annotations
from pathlib import Path
from typing import Callable

from nutshell.core.loader import BaseLoader
from nutshell.session_engine.entity_config import AgentConfig
from nutshell.core.agent import Agent
from nutshell.skill_engine.loader import SkillLoader
from nutshell.tool_engine.loader import ToolLoader


def _load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


class AgentLoader(BaseLoader[Agent]):
    """Load a complete Agent from an entity directory containing config.yaml.

    Each entity is fully self-contained — prompts in config.yaml, tools in
    tools.md, skills in skills.md.
    """

    def __init__(self, impl_registry: dict[str, Callable] | None = None) -> None:
        self._impl_registry = impl_registry or {}

    def load(self, path: Path) -> Agent:
        """Load agent from a directory containing config.yaml."""
        path = Path(path)
        config = AgentConfig.from_path(path)
        manifest = config.manifest

        child_prompts = manifest.get("prompts") or {}

        def load_prompt_key(key: str) -> str:
            rel = child_prompts.get(key)
            if rel:
                p = path / rel
                return _load_prompt(p) if p.exists() else ""
            return ""

        system_prompt    = load_prompt_key("system")
        # New keys with fallback to old keys for backward compat
        task_prompt      = load_prompt_key("task") or load_prompt_key("heartbeat")
        env_template     = load_prompt_key("env") or load_prompt_key("session_context")

        # Skills from skills.md (skillhub)
        skill_loader = SkillLoader()
        skills_md = path / "skills.md"
        if skills_md.exists():
            skills = skill_loader.load_from_skills_md(skills_md)
        else:
            skills = []

        # Tools from tools.md (toolhub)
        tool_loader = ToolLoader(impl_registry=self._impl_registry, skills=skills)
        tools_md = path / "tools.md"
        if tools_md.exists():
            tools = tool_loader.load_from_tool_md(tools_md)
        else:
            tools = []

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
            task_prompt=task_prompt,
            env_template=env_template,
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
