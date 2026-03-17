from __future__ import annotations
from pathlib import Path
from typing import Callable

from nutshell.runtime.loaders import BaseLoader
from nutshell.core.agent import Agent
from nutshell.runtime.loaders.skill import SkillLoader
from nutshell.runtime.loaders.tool import ToolLoader


def _load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


class AgentLoader(BaseLoader[Agent]):
    """Load a complete Agent from an entity directory containing agent.yaml.

    Supports arbitrarily deep entity inheritance via ``extends`` in agent.yaml.
    agent.yaml always contains the full set of fields. A null value signals
    "inherit from parent":

        prompts:
          system:          # null  → inherit from parent (recursively resolved)
          heartbeat:       # null  → inherit
          session_context: prompts/session_context.md  # value → load from this entity

        tools:             # null  → inherit parent's tools list
        skills: []         # []    → explicitly no skills (do NOT inherit)
        skills:            # null  → inherit parent's skills list
          - skills/foo     # explicit list → resolve files child-first along ancestor chain

    Rules:
    - Inheritance is recursive: A extends B extends C works correctly.
    - Prompts: null → use parent's already-resolved value.
               string value → load from this entity's directory.
    - tools/skills: None (null/absent) → inherit parent's resolved list.
                    [] → explicitly empty (no inheritance).
                    [list] → resolve each file child-first along the full ancestor chain.
    - model/provider: null/absent → inherit from parent; fallback to built-in defaults.

    Args:
        impl_registry: Optional dict mapping tool name -> callable.
    """

    def __init__(self, impl_registry: dict[str, Callable] | None = None) -> None:
        self._impl_registry = impl_registry or {}

    # ── Public API ────────────────────────────────────────────────────────────

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

        # ── Resolve parent (recursive) ────────────────────────────────────────
        parent: Agent | None = None
        extends = manifest.get("extends")
        if extends:
            candidate = path.parent / extends
            if not (candidate / "agent.yaml").exists():
                raise FileNotFoundError(
                    f"Entity '{path.name}' extends '{extends}' "
                    f"but parent not found at: {candidate}"
                )
            parent = AgentLoader(self._impl_registry).load(candidate)

        # Ancestor directory chain: [this, parent, grandparent, ...]
        # Used for child-first file resolution across the full inheritance depth.
        ancestor_dirs = self._ancestor_dirs(path)

        def resolve_file(rel: str) -> Path | None:
            """Return the first existing path for rel, walking the ancestor chain."""
            for d in ancestor_dirs:
                p = d / rel
                if p.exists():
                    return p
            return None

        # ── Prompts ──────────────────────────────────────────────────────────
        child_prompts = manifest.get("prompts") or {}

        def load_prompt_key(key: str, parent_attr: str) -> str:
            rel = child_prompts.get(key)
            if rel:
                p = path / rel
                return _load_prompt(p) if p.exists() else ""
            # null/absent → use parent's already-resolved value
            if parent is not None:
                return getattr(parent, parent_attr) or ""
            return ""

        system_prompt           = load_prompt_key("system",          "system_prompt")
        heartbeat_prompt        = load_prompt_key("heartbeat",       "heartbeat_prompt")
        session_context_template = load_prompt_key("session_context", "session_context_template")

        # ── Tools ─────────────────────────────────────────────────────────────
        raw_tools = manifest.get("tools")
        if raw_tools is None:
            # null → inherit parent's fully-resolved list
            tools = list(parent.tools) if parent is not None else []
        else:
            tool_loader = ToolLoader(impl_registry=self._impl_registry)
            tools = [
                tool_loader.load(resolved)
                for t in (raw_tools or [])
                if (resolved := resolve_file(t)) is not None
            ]

        # ── Skills ────────────────────────────────────────────────────────────
        raw_skills = manifest.get("skills")
        if raw_skills is None:
            # null → inherit parent's fully-resolved list
            skills = list(parent.skills) if parent is not None else []
        else:
            skills = [
                SkillLoader().load(resolved)
                for s in (raw_skills or [])
                if (resolved := resolve_file(s)) is not None
            ]

        # ── Model / Provider ─────────────────────────────────────────────────
        model = manifest.get("model")
        if not model:
            model = parent.model if parent is not None else "claude-sonnet-4-6"

        provider_str = manifest.get("provider")
        if not provider_str and parent is not None:
            try:
                from nutshell.runtime.provider_factory import provider_name
                provider_str = provider_name(parent._provider)
            except Exception:
                pass
        provider_str = provider_str or "anthropic"

        # ── Assemble ──────────────────────────────────────────────────────────
        agent = Agent(
            system_prompt=system_prompt,
            tools=tools,
            skills=skills,
            model=model,
            release_policy=manifest.get("release_policy", "persistent"),
            max_iterations=manifest.get("max_iterations", 20),
            heartbeat_prompt=heartbeat_prompt,
            session_context_template=session_context_template,
        )

        try:
            from nutshell.runtime.provider_factory import resolve_provider
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

    # ── Private ───────────────────────────────────────────────────────────────

    def _ancestor_dirs(self, path: Path) -> list[Path]:
        """Return [path, parent, grandparent, ...] by walking the extends chain."""
        try:
            import yaml
        except ImportError:
            return [path]

        dirs: list[Path] = []
        current = path
        while True:
            dirs.append(current)
            mpath = current / "agent.yaml"
            if not mpath.exists():
                break
            manifest = yaml.safe_load(mpath.read_text(encoding="utf-8")) or {}
            extends = manifest.get("extends")
            if not extends:
                break
            parent = current.parent / extends
            if not (parent / "agent.yaml").exists():
                break
            current = parent
        return dirs
