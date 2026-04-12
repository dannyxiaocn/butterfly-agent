from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from nutshell.session_engine.agent_loader import AgentLoader


class AgentLoaderUnitTests(unittest.TestCase):
    def test_load_flat_entity_with_explicit_prompts(self) -> None:
        """AgentLoader loads a self-contained entity with no inheritance."""
        with TemporaryDirectory() as td:
            root = Path(td)
            entity_dir = root / "myagent"
            (entity_dir / "prompts").mkdir(parents=True)
            (entity_dir / "prompts" / "system.md").write_text("system prompt", encoding="utf-8")
            (entity_dir / "agent.yaml").write_text(
                "name: myagent\nmodel: claude-sonnet-4-6\nprovider: anthropic\nprompts:\n  system: prompts/system.md\ntools: []\nskills: []\n",
                encoding="utf-8",
            )
            agent = AgentLoader().load(entity_dir)
        self.assertEqual(agent.system_prompt, "system prompt")
        self.assertEqual(agent.model, "claude-sonnet-4-6")

    def test_load_falls_back_to_default_model_when_absent(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            entity_dir = root / "minimal"
            entity_dir.mkdir()
            (entity_dir / "agent.yaml").write_text("name: minimal\n", encoding="utf-8")
            agent = AgentLoader().load(entity_dir)
        self.assertEqual(agent.model, "claude-sonnet-4-6")
