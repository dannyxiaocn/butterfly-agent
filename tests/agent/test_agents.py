from __future__ import annotations

import unittest

from butterfly.session_engine.agent_loader import AgentLoader

from conftest import REPO_ROOT

AGENT_ROOT = REPO_ROOT / "agenthub"
ACTIVE_AGENTS = ["agent", "butterfly_dev"]


class AgentUnitTests(unittest.TestCase):
    def test_active_agents_load_without_errors(self) -> None:
        loader = AgentLoader()
        for agent in ACTIVE_AGENTS:
            loaded = loader.load(AGENT_ROOT / agent)
            self.assertTrue(loaded.model)
