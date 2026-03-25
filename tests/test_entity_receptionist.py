"""Tests for entity/receptionist — validate entity loading, inheritance, skill resolution."""
from __future__ import annotations

import yaml
import pytest
from pathlib import Path

ENTITY_DIR = Path(__file__).parent.parent / "entity"
RECEPTIONIST_DIR = ENTITY_DIR / "receptionist"


# ── YAML loading ──────────────────────────────────────────────────────────────

class TestAgentYaml:
    """Verify the receptionist agent.yaml is well-formed."""

    @pytest.fixture
    def manifest(self) -> dict:
        text = (RECEPTIONIST_DIR / "agent.yaml").read_text(encoding="utf-8")
        return yaml.safe_load(text)

    def test_agent_yaml_exists(self):
        assert (RECEPTIONIST_DIR / "agent.yaml").exists()

    def test_name_is_receptionist(self, manifest):
        assert manifest["name"] == "receptionist"

    def test_extends_agent(self, manifest):
        assert manifest["extends"] == "agent"

    def test_has_system_prompt_path(self, manifest):
        prompts = manifest.get("prompts", {})
        assert prompts.get("system") == "prompts/system.md"

    def test_system_prompt_file_exists(self):
        assert (RECEPTIONIST_DIR / "prompts" / "system.md").exists()

    def test_system_prompt_not_empty(self):
        content = (RECEPTIONIST_DIR / "prompts" / "system.md").read_text()
        assert len(content.strip()) > 100  # substantial prompt

    def test_tools_list(self, manifest):
        tools = manifest.get("tools", [])
        assert "tools/bash.json" in tools
        assert "tools/spawn_session.json" in tools
        assert "tools/send_to_session.json" in tools

    def test_skills_include_delegate(self, manifest):
        skills = manifest.get("skills", [])
        assert "skills/delegate" in skills

    def test_skills_include_multi_agent(self, manifest):
        skills = manifest.get("skills", [])
        assert "skills/multi-agent" in skills

    def test_release_policy(self, manifest):
        assert manifest.get("release_policy") == "persistent"

    def test_max_iterations(self, manifest):
        assert manifest.get("max_iterations", 20) == 20


# ── Delegate skill ────────────────────────────────────────────────────────────

class TestDelegateSkill:
    """Verify the delegate skill is well-formed."""

    SKILL_PATH = RECEPTIONIST_DIR / "skills" / "delegate" / "SKILL.md"

    def test_skill_file_exists(self):
        assert self.SKILL_PATH.exists()

    def test_skill_has_frontmatter(self):
        content = self.SKILL_PATH.read_text()
        assert content.startswith("---")
        # Extract frontmatter
        parts = content.split("---", 2)
        assert len(parts) >= 3
        fm = yaml.safe_load(parts[1])
        assert fm["name"] == "delegate"
        assert "description" in fm

    def test_skill_mentions_spawn_session(self):
        content = self.SKILL_PATH.read_text()
        assert "spawn_session" in content

    def test_skill_mentions_send_to_session(self):
        content = self.SKILL_PATH.read_text()
        assert "send_to_session" in content

    def test_skill_mentions_monitoring(self):
        content = self.SKILL_PATH.read_text()
        assert "monitor" in content.lower() or "progress" in content.lower()


# ── AgentLoader integration ──────────────────────────────────────────────────

class TestAgentLoaderIntegration:
    """Verify the receptionist entity loads through AgentLoader without errors."""

    def test_load_receptionist(self):
        from nutshell.llm_engine.loader import AgentLoader
        loader = AgentLoader()
        agent = loader.load(RECEPTIONIST_DIR)
        assert agent is not None

    def test_loaded_agent_has_system_prompt(self):
        from nutshell.llm_engine.loader import AgentLoader
        loader = AgentLoader()
        agent = loader.load(RECEPTIONIST_DIR)
        assert "receptionist" in agent.system_prompt.lower()

    def test_loaded_agent_has_tools(self):
        from nutshell.llm_engine.loader import AgentLoader
        loader = AgentLoader()
        agent = loader.load(RECEPTIONIST_DIR)
        tool_names = {t.name for t in agent.tools}
        assert "bash" in tool_names
        assert "spawn_session" in tool_names
        assert "send_to_session" in tool_names

    def test_loaded_agent_has_skills(self):
        from nutshell.llm_engine.loader import AgentLoader
        loader = AgentLoader()
        agent = loader.load(RECEPTIONIST_DIR)
        skill_names = {s.name for s in agent.skills}
        assert "delegate" in skill_names
        assert "multi-agent" in skill_names

    def test_loaded_agent_inherits_heartbeat_from_parent(self):
        from nutshell.llm_engine.loader import AgentLoader
        loader = AgentLoader()
        agent = loader.load(RECEPTIONIST_DIR)
        # heartbeat is inherited from agent entity — should be non-empty
        assert agent.heartbeat_prompt  # inherited, not empty

    def test_loaded_agent_model(self):
        from nutshell.llm_engine.loader import AgentLoader
        loader = AgentLoader()
        agent = loader.load(RECEPTIONIST_DIR)
        assert agent.model == "claude-sonnet-4-6"

    def test_no_heavy_tools(self):
        """Receptionist should NOT have git_checkpoint or propose_entity_update —
        those are for agents that do real dev work."""
        from nutshell.llm_engine.loader import AgentLoader
        loader = AgentLoader()
        agent = loader.load(RECEPTIONIST_DIR)
        tool_names = {t.name for t in agent.tools}
        assert "git_checkpoint" not in tool_names
        assert "propose_entity_update" not in tool_names


# ── Entity directory structure ────────────────────────────────────────────────

class TestEntityStructure:
    """Verify directory layout matches conventions."""

    def test_entity_dir_exists(self):
        assert RECEPTIONIST_DIR.is_dir()

    def test_prompts_dir_exists(self):
        assert (RECEPTIONIST_DIR / "prompts").is_dir()

    def test_skills_dir_exists(self):
        assert (RECEPTIONIST_DIR / "skills").is_dir()

    def test_delegate_skill_dir_exists(self):
        assert (RECEPTIONIST_DIR / "skills" / "delegate").is_dir()

    def test_no_tools_dir(self):
        """Receptionist inherits tools from parent agent — no local tools/ needed."""
        # The tools are referenced by path but resolved via ancestor_dirs
        # receptionist itself doesn't need a tools/ dir
        pass

    def test_all_tool_refs_resolve(self):
        """Every tool path in agent.yaml should resolve through ancestor chain."""
        manifest = yaml.safe_load(
            (RECEPTIONIST_DIR / "agent.yaml").read_text()
        )
        for tool_rel in manifest.get("tools", []):
            # Should exist in receptionist dir or agent dir
            found = (
                (RECEPTIONIST_DIR / tool_rel).exists()
                or (ENTITY_DIR / "agent" / tool_rel).exists()
            )
            assert found, f"Tool '{tool_rel}' not found in ancestor chain"

    def test_all_skill_refs_resolve(self):
        """Every skill path should resolve through ancestor chain."""
        manifest = yaml.safe_load(
            (RECEPTIONIST_DIR / "agent.yaml").read_text()
        )
        for skill_rel in manifest.get("skills", []):
            found = (
                (RECEPTIONIST_DIR / skill_rel / "SKILL.md").exists()
                or (ENTITY_DIR / "agent" / skill_rel / "SKILL.md").exists()
            )
            assert found, f"Skill '{skill_rel}' not found in ancestor chain"
