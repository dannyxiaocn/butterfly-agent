from __future__ import annotations

import unittest
from pathlib import Path

from nutshell.session_engine.agent_loader import AgentLoader
from porter_test_support import repo_root_from


REPO_ROOT = repo_root_from(Path(__file__))

ENTITY_ROOT = REPO_ROOT / "entity"
ACTIVE_ENTITIES = ["agent", "nutshell_dev", "nutshell_dev_codex", "porters"]


DOCS_ROOT = REPO_ROOT / "docs" / "entity"


class EntityUnitTests(unittest.TestCase):
    def test_active_entity_docs_follow_contract(self) -> None:
        design = (DOCS_ROOT / "design.md").read_text(encoding="utf-8")
        self.assertIn("Entity", design)
        for entity in ACTIVE_ENTITIES:
            entity_docs = DOCS_ROOT / entity
            self.assertTrue(entity_docs.exists(), f"missing docs for {entity}")
            self.assertTrue((entity_docs / "design.md").exists(), f"missing design.md for {entity}")
            self.assertTrue((entity_docs / "impl.md").exists(), f"missing impl.md for {entity}")
            self.assertTrue((entity_docs / "todo.md").exists(), f"missing todo.md for {entity}")

    def test_active_entities_load_without_inheritance_errors(self) -> None:
        loader = AgentLoader()
        for entity in ACTIVE_ENTITIES:
            agent = loader.load(ENTITY_ROOT / entity)
            self.assertTrue(agent.model)
