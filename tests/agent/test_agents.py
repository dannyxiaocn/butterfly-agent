from __future__ import annotations

import unittest
from pathlib import Path

from butterfly.session_engine.agent_loader import AgentLoader


from conftest import REPO_ROOT

AGENT_ROOT = REPO_ROOT / "agenthub"
ACTIVE_AGENTS = ["agent", "butterfly_dev"]


DOCS_ROOT = REPO_ROOT / "docs" / "agent"


class AgentUnitTests(unittest.TestCase):
    def test_active_agent_docs_follow_contract(self) -> None:
        design = (DOCS_ROOT / "design.md").read_text(encoding="utf-8")
        self.assertIn("Agent", design)
        for agent in ACTIVE_AGENTS:
            agent_docs = DOCS_ROOT / agent
            self.assertTrue(agent_docs.exists(), f"missing docs for {agent}")
            self.assertTrue((agent_docs / "design.md").exists(), f"missing design.md for {agent}")
            self.assertTrue((agent_docs / "impl.md").exists(), f"missing impl.md for {agent}")
            self.assertTrue((agent_docs / "todo.md").exists(), f"missing todo.md for {agent}")

    def test_active_agents_load_without_errors(self) -> None:
        loader = AgentLoader()
        for agent in ACTIVE_AGENTS:
            loaded = loader.load(AGENT_ROOT / agent)
            self.assertTrue(loaded.model)
