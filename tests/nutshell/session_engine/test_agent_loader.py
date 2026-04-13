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
            # Use config.yaml (new name)
            (entity_dir / "config.yaml").write_text(
                "name: myagent\nmodel: claude-sonnet-4-6\nprovider: anthropic\nprompts:\n  system: prompts/system.md\n",
                encoding="utf-8",
            )
            (entity_dir / "tools.md").write_text("", encoding="utf-8")
            (entity_dir / "skills.md").write_text("", encoding="utf-8")
            agent = AgentLoader().load(entity_dir)
        self.assertEqual(agent.system_prompt, "system prompt")
        self.assertEqual(agent.model, "claude-sonnet-4-6")

    def test_load_requires_config_yaml(self) -> None:
        """AgentLoader raises FileNotFoundError when config.yaml is absent."""
        with TemporaryDirectory() as td:
            root = Path(td)
            entity_dir = root / "minimal"
            entity_dir.mkdir()
            with self.assertRaises(FileNotFoundError):
                AgentLoader().load(entity_dir)

    def test_load_with_task_and_env_prompts(self) -> None:
        """AgentLoader reads new task + env prompt keys from config.yaml."""
        with TemporaryDirectory() as td:
            root = Path(td)
            entity_dir = root / "full"
            (entity_dir / "prompts").mkdir(parents=True)
            (entity_dir / "prompts" / "system.md").write_text("sys", encoding="utf-8")
            (entity_dir / "prompts" / "task.md").write_text("task prompt", encoding="utf-8")
            (entity_dir / "prompts" / "env.md").write_text("env template", encoding="utf-8")
            (entity_dir / "config.yaml").write_text(
                "name: full\nmodel: claude-sonnet-4-6\nprovider: anthropic\n"
                "prompts:\n  system: prompts/system.md\n  task: prompts/task.md\n  env: prompts/env.md\n",
                encoding="utf-8",
            )
            (entity_dir / "tools.md").write_text("", encoding="utf-8")
            (entity_dir / "skills.md").write_text("", encoding="utf-8")
            agent = AgentLoader().load(entity_dir)
        self.assertEqual(agent.task_prompt, "task prompt")
        self.assertEqual(agent.env_template, "env template")
