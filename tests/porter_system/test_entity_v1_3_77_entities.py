from __future__ import annotations

import unittest
from pathlib import Path

from nutshell.session_engine.agent_loader import AgentLoader


REPO_ROOT = Path(__file__).resolve()
for candidate in (REPO_ROOT.parent, *REPO_ROOT.parents):
    if (candidate / "pyproject.toml").exists():
        REPO_ROOT = candidate
        break

ENTITY_ROOT = REPO_ROOT / "entity"
ACTIVE_ENTITIES = ["agent", "nutshell_dev", "nutshell_dev_codex"]


class EntityUnitTests(unittest.TestCase):
    def test_active_entity_readmes_follow_contract(self) -> None:
        catalog = (ENTITY_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Entity Catalog", catalog)
        for entity in ACTIVE_ENTITIES:
            self.assertIn(f"`{entity}`", catalog)
            readme = (ENTITY_ROOT / entity / "README.md").read_text(encoding="utf-8")
            self.assertTrue(readme.startswith(f"# {entity}"))
            self.assertIn("## Purpose", readme)
            self.assertIn("## Notes", readme)

    def test_active_entities_load_without_inheritance_errors(self) -> None:
        loader = AgentLoader()
        for entity in ACTIVE_ENTITIES:
            agent = loader.load(ENTITY_ROOT / entity)
            self.assertTrue(agent.model)

