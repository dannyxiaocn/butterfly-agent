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

    Supports entity inheritance via the ``extends`` field in agent.yaml.
    agent.yaml always contains the full set of fields. A null value signals
    "inherit from parent":

        prompts:
          system:          # null  → load from parent entity's prompts.system path
          heartbeat:       # null  → inherit
          session_context: prompts/session_context.md  # value → load from this entity

        tools:             # null  → inherit parent's tools list
        skills: []         # []    → explicitly no skills (do NOT inherit)

    Rules:
    - Prompts: null value → resolve path from parent manifest, load from parent dir.
               string value → load from this entity's dir.
    - tools/skills: None (key absent or null) → inherit parent's list, load files
               from parent dir.  [] or a list → use as-is, load files from this dir.

    Args:
        impl_registry: Optional dict mapping tool name -> callable.
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

        # ── Resolve parent ────────────────────────────────────────────────────
        parent_path: Path | None = None
        parent_manifest: dict = {}
        extends = manifest.get("extends")
        if extends:
            candidate = path.parent / extends
            if not (candidate / "agent.yaml").exists():
                raise FileNotFoundError(
                    f"Entity '{path.name}' extends '{extends}' "
                    f"but parent not found at: {candidate}"
                )
            parent_path = candidate
            parent_manifest = yaml.safe_load(
                (parent_path / "agent.yaml").read_text(encoding="utf-8")
            ) or {}

        child_prompts = manifest.get("prompts") or {}
        parent_prompts = parent_manifest.get("prompts") or {}

        # ── Prompts ──────────────────────────────────────────────────────────
        def load_prompt_key(key: str) -> str:
            rel = child_prompts.get(key)  # None if key absent or value is null
            if rel:
                # Explicit path → load from this entity's directory
                p = path / rel
                return _load_prompt(p) if p.exists() else ""
            # Null/absent → inherit: use parent's path, load from parent's directory
            parent_rel = parent_prompts.get(key)
            if parent_rel and parent_path:
                p = parent_path / parent_rel
                return _load_prompt(p) if p.exists() else ""
            return ""

        system_prompt = load_prompt_key("system")
        heartbeat_prompt = load_prompt_key("heartbeat")
        session_context_template = load_prompt_key("session_context")

        def resolve_file(rel: str) -> Path | None:
            """Child directory first, then parent directory."""
            p = path / rel
            if p.exists():
                return p
            if parent_path:
                p = parent_path / rel
                if p.exists():
                    return p
            return None

        # ── Tools ─────────────────────────────────────────────────────────────
        # None (null/absent) → inherit parent's list. [] or explicit list → use as-is.
        # Each path in the list resolves child-first, parent-fallback.
        raw_tools = manifest.get("tools")
        if raw_tools is None and parent_path:
            tools_cfg = parent_manifest.get("tools") or []
        else:
            tools_cfg = raw_tools or []

        tool_loader = ToolLoader(impl_registry=self._impl_registry)
        tools = [
            tool_loader.load(resolved)
            for t in tools_cfg
            if (resolved := resolve_file(t)) is not None
        ]

        # ── Skills ────────────────────────────────────────────────────────────
        raw_skills = manifest.get("skills")
        if raw_skills is None and parent_path:
            skills_cfg = parent_manifest.get("skills") or []
        else:
            skills_cfg = raw_skills or []

        skills = [
            SkillLoader().load(resolved)
            for s in skills_cfg
            if (resolved := resolve_file(s)) is not None
        ]

        # ── Assemble ──────────────────────────────────────────────────────────
        agent = Agent(
            system_prompt=system_prompt,
            tools=tools,
            skills=skills,
            model=manifest.get("model", "claude-sonnet-4-6"),
            release_policy=manifest.get("release_policy", "persistent"),
            max_iterations=manifest.get("max_iterations", 20),
            heartbeat_prompt=heartbeat_prompt,
            session_context_template=session_context_template,
        )

        provider_str = manifest.get("provider", "anthropic")
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
