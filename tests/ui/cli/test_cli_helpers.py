from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ui.cli.main import _parse_inject_memory, _write_inject_memory
from ui.cli.new_agent import create_entity


class CliHelpersTest(unittest.TestCase):
    def test_parse_and_write_inject_memory(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "memory.txt"
            source.write_text("from file", encoding="utf-8")
            parsed = _parse_inject_memory(["alpha=value", f"beta=@{source}"])
            self.assertEqual(parsed["alpha"], "value")
            self.assertEqual(parsed["beta"], "from file")

            session_dir = root / "sessions" / "demo"
            _write_inject_memory(session_dir, parsed)
            self.assertEqual(
                (session_dir / "core" / "memory" / "alpha.md").read_text(encoding="utf-8"),
                "value",
            )

    def test_create_entity_scaffolds_blank_and_init_from_layouts(self) -> None:
        with TemporaryDirectory() as tmp:
            entity_root = Path(tmp) / "entity"
            (entity_root / "agent" / "prompts").mkdir(parents=True)
            (entity_root / "agent" / "tools").mkdir()
            (entity_root / "agent" / "agent.yaml").write_text("name: agent\n", encoding="utf-8")
            (entity_root / "agent" / "prompts" / "system.md").write_text("sys", encoding="utf-8")
            (entity_root / "agent" / "prompts" / "heartbeat.md").write_text("beat", encoding="utf-8")
            (entity_root / "agent" / "prompts" / "session.md").write_text("sess", encoding="utf-8")
            (entity_root / "agent" / "tools" / "bash.json").write_text('{"name":"bash"}', encoding="utf-8")
            (entity_root / "agent" / "tools" / "web_search.json").write_text('{"name":"web_search"}', encoding="utf-8")
            blank = create_entity("blank", entity_root, None)
            child = create_entity("child", entity_root, "agent")
            # Blank entity: empty prompt placeholders, no tools
            self.assertTrue((blank / "prompts" / "system.md").exists())
            self.assertFalse((blank / "tools" / "bash.json").exists())
            # init_from entity: all files copied from source
            self.assertTrue((child / "tools" / "bash.json").exists())
            self.assertTrue((child / "tools" / "web_search.json").exists())
            self.assertEqual((child / "prompts" / "system.md").read_text(), "sys")


if __name__ == "__main__":
    unittest.main()
