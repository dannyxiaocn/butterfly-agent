from __future__ import annotations
from pathlib import Path
from typing import Callable

from nutshell.core.loader import AgentConfig, BaseLoader
from nutshell.core.agent import Agent
from nutshell.skill_engine.loader import SkillLoader
from nutshell.tool_engine.loader import ToolLoader


def _load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


class AgentLoader(BaseLoader[Agent]):
    """Load a complete Agent from an entity directory containing agent.yaml.

    Supports arbitrarily deep entity inheritance via ``extends`` in agent.yaml.
    """

    def __init__(self, impl_registry: dict[str, Callable] | None = None) -> None:
        self._impl_registry = impl_registry or {}

    def _ancestor_dirs(self, path: Path) -> list[Path]:
        """Return [path, parent, grandparent, ...] by walking the extends chain."""
        dirs: list[Path] = []
        current = path
        while True:
            dirs.append(current)
            try:
                config = AgentConfig.from_path(current)
            except Exception:
                break
            extends = config.extends
            if not extends:
                break
            parent = current.parent / extends
            if not (parent / "agent.yaml").exists():
                break
            current = parent
        return dirs

    def load(self, path: Path) -> Agent:
        """Load agent from a directory containing agent.yaml."""
        path = Path(path)
        config = AgentConfig.from_path(path)
        manifest = config.manifest

        parent: Agent | None = None
        extends = config.extends
        if extends:
            candidate = path.parent / extends
            if not (candidate / "agent.yaml").exists():
                raise FileNotFoundError(
                    f"Entity '{path.name}' extends '{extends}' "
                    f"but parent not found at: {candidate}"
                )
            parent = AgentLoader(self._impl_registry).load(candidate)

        ancestor_dirs = self._ancestor_dirs(path)

        def resolve_file(rel: str) -> Path | None:
            for d in ancestor_dirs:
                p = d / rel
                if p.exists():
                    return p
            return None

        child_prompts = manifest.get("prompts") or {}

        def load_prompt_key(key: str, parent_attr: str) -> str:
            rel = child_prompts.get(key)
            if rel:
                p = path / rel
                return _load_prompt(p) if p.exists() else ""
            if parent is not None:
                return getattr(parent, parent_attr) or ""
            return ""

        system_prompt            = load_prompt_key("system",          "system_prompt")
        heartbeat_prompt         = load_prompt_key("heartbeat",       "heartbeat_prompt")
        session_context_template = load_prompt_key("session_context", "session_context_template")

        raw_tools = manifest.get("tools")
        if raw_tools is None:
            tools = list(parent.tools) if parent is not None else []
        else:
            tool_loader = ToolLoader(impl_registry=self._impl_registry)
            tools = [
                tool_loader.load(resolved)
                for t in (raw_tools or [])
                if (resolved := resolve_file(t)) is not None
            ]

        raw_skills = manifest.get("skills")
        if raw_skills is None:
            skills = list(parent.skills) if parent is not None else []
        else:
            skills = [
                SkillLoader().load(resolved)
                for s in (raw_skills or [])
                if (resolved := resolve_file(s)) is not None
            ]

        model = manifest.get("model")
        if not model:
            model = parent.model if parent is not None else "claude-sonnet-4-6"

        provider_str = manifest.get("provider")
        if not provider_str and parent is not None:
            try:
                from nutshell.llm_engine.registry import provider_name
                provider_str = provider_name(parent._provider)
            except Exception:
                pass
        provider_str = provider_str or "anthropic"

        fallback_model = manifest.get("fallback_model", "")
        fallback_provider = manifest.get("fallback_provider", "")
        # Inherit fallback from parent if not overridden
        if not fallback_model and parent is not None:
            fallback_model = getattr(parent, "fallback_model", "") or ""
        if not fallback_provider and parent is not None:
            fallback_provider = getattr(parent, "_fallback_provider_str", "") or ""

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
        """Load all agents from subdirectories that contain agent.yaml."""
        directory = Path(directory)
        agents = []
        for subdir in sorted(directory.iterdir()):
            if subdir.is_dir() and (subdir / "agent.yaml").exists():
                agents.append(self.load(subdir))
        return agents

    def load_from_entity(self, name: str) -> Agent:
        """Load agent by entity name from the default entity/ directory."""
        from pathlib import Path
        entity_dir = Path(__file__).parent.parent.parent / "entity" / name
        return self.load(entity_dir)
