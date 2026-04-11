from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from nutshell.session_engine.agent_loader import AgentLoader


class AgentLoaderUnitTests(unittest.TestCase):
    def test_cyclic_extends_raises_clear_error(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            for name, parent in (("a", "b"), ("b", "a")):
                entity_dir = root / name
                entity_dir.mkdir()
                (entity_dir / "agent.yaml").write_text(f"extends: {parent}\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "inheritance cycle"):
                AgentLoader().load(root / "a")

