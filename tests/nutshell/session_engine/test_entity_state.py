from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nutshell.session_engine.entity_state import get_meta_dir, sync_from_entity


def _write_entity(entity_root: Path, name: str) -> Path:
    entity_dir = entity_root / name
    (entity_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (entity_dir / "playground").mkdir(exist_ok=True)
    (entity_dir / "memory").mkdir(exist_ok=True)
    (entity_dir / "prompts" / "system.md").write_text("system", encoding="utf-8")
    (entity_dir / "prompts" / "heartbeat.md").write_text("heartbeat", encoding="utf-8")
    (entity_dir / "prompts" / "session.md").write_text("session", encoding="utf-8")
    (entity_dir / "agent.yaml").write_text(
        "name: {}\nprompts:\n  system: prompts/system.md\n  heartbeat: prompts/heartbeat.md\n  session_context: prompts/session.md\n".format(name),
        encoding="utf-8",
    )
    return entity_dir


class EntityStateUnitTests(unittest.TestCase):
    def test_sync_from_entity_bootstraps_memory_when_absent(self) -> None:
        with TemporaryDirectory() as td, patch(
            "nutshell.session_engine.entity_state._create_meta_venv",
            side_effect=lambda p: p / ".venv",
        ):
            root = Path(td)
            entity_root = root / "entity"
            sessions_base = root / "sessions"
            entity = _write_entity(entity_root, "myagent")
            (entity / "memory.md").write_text("entity memory", encoding="utf-8")
            (entity / "memory" / "layer.md").write_text("layer content", encoding="utf-8")

            sync_from_entity("myagent", entity_base=entity_root, s_base=sessions_base)

            meta_dir = get_meta_dir("myagent", s_base=sessions_base)
            self.assertEqual((meta_dir / "core" / "memory.md").read_text(encoding="utf-8"), "entity memory")
            self.assertEqual((meta_dir / "core" / "memory" / "layer.md").read_text(encoding="utf-8"), "layer content")

    def test_sync_from_entity_does_not_overwrite_existing_meta_memory(self) -> None:
        with TemporaryDirectory() as td, patch(
            "nutshell.session_engine.entity_state._create_meta_venv",
            side_effect=lambda p: p / ".venv",
        ):
            root = Path(td)
            entity_root = root / "entity"
            sessions_base = root / "sessions"
            entity = _write_entity(entity_root, "myagent")
            (entity / "memory.md").write_text("entity memory", encoding="utf-8")

            # First sync — bootstraps
            sync_from_entity("myagent", entity_base=entity_root, s_base=sessions_base)
            meta_dir = get_meta_dir("myagent", s_base=sessions_base)
            # Meta memory is now "entity memory"
            (meta_dir / "core" / "memory.md").write_text("meta own memory", encoding="utf-8")

            # Second sync — should NOT overwrite because meta memory is non-empty
            sync_from_entity("myagent", entity_base=entity_root, s_base=sessions_base)
            self.assertEqual((meta_dir / "core" / "memory.md").read_text(encoding="utf-8"), "meta own memory")

    def test_sync_from_entity_bootstraps_playground_files(self) -> None:
        with TemporaryDirectory() as td, patch(
            "nutshell.session_engine.entity_state._create_meta_venv",
            side_effect=lambda p: p / ".venv",
        ):
            root = Path(td)
            entity_root = root / "entity"
            sessions_base = root / "sessions"
            entity = _write_entity(entity_root, "myagent")
            (entity / "playground" / "seed.txt").write_text("seed content", encoding="utf-8")

            sync_from_entity("myagent", entity_base=entity_root, s_base=sessions_base)

            meta_dir = get_meta_dir("myagent", s_base=sessions_base)
            self.assertEqual((meta_dir / "playground" / "seed.txt").read_text(encoding="utf-8"), "seed content")
