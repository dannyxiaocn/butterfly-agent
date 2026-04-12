from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nutshell.session_engine.entity_state import get_meta_dir, sync_from_entity


def _write_entity(entity_root: Path, name: str, *, extends: str | None = None) -> Path:
    entity_dir = entity_root / name
    (entity_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (entity_dir / "playground").mkdir(exist_ok=True)
    (entity_dir / "memory").mkdir(exist_ok=True)
    (entity_dir / "prompts" / "system.md").write_text("system", encoding="utf-8")
    (entity_dir / "prompts" / "heartbeat.md").write_text("heartbeat", encoding="utf-8")
    (entity_dir / "prompts" / "session.md").write_text("session", encoding="utf-8")
    lines = []
    if extends:
        lines.append(f"extends: {extends}")
    lines.extend(
        [
            "prompts:",
            "  system: prompts/system.md",
            "  heartbeat: prompts/heartbeat.md",
            "  session_context: prompts/session.md",
        ]
    )
    (entity_dir / "agent.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return entity_dir


class EntityStateUnitTests(unittest.TestCase):
    def test_existing_meta_memory_does_not_block_inherited_memory_sync(self) -> None:
        with TemporaryDirectory() as td, patch(
            "nutshell.session_engine.entity_state._create_meta_venv",
            side_effect=lambda p: p / ".venv",
        ):
            root = Path(td)
            entity_root = root / "entity"
            sessions_base = root / "sessions"
            parent = _write_entity(entity_root, "parent")
            child = _write_entity(entity_root, "child", extends="parent")
            (parent / "memory.md").write_text("parent memory", encoding="utf-8")

            sync_from_entity("child", entity_base=entity_root, s_base=sessions_base)
            (parent / "memory" / "later.md").write_text("late memory", encoding="utf-8")
            sync_from_entity("child", entity_base=entity_root, s_base=sessions_base)

            meta_dir = get_meta_dir("child", s_base=sessions_base)
            self.assertEqual((meta_dir / "core" / "memory.md").read_text(encoding="utf-8"), "parent memory")
            self.assertEqual((meta_dir / "core" / "memory" / "later.md").read_text(encoding="utf-8"), "late memory")

    def test_existing_meta_memory_does_not_block_inherited_playground_sync(self) -> None:
        with TemporaryDirectory() as td, patch(
            "nutshell.session_engine.entity_state._create_meta_venv",
            side_effect=lambda p: p / ".venv",
        ):
            root = Path(td)
            entity_root = root / "entity"
            sessions_base = root / "sessions"
            parent = _write_entity(entity_root, "parent")
            child = _write_entity(entity_root, "child", extends="parent")
            (parent / "memory.md").write_text("parent memory", encoding="utf-8")

            sync_from_entity("child", entity_base=entity_root, s_base=sessions_base)
            (parent / "playground" / "later.txt").write_text("late playground", encoding="utf-8")
            sync_from_entity("child", entity_base=entity_root, s_base=sessions_base)

            meta_dir = get_meta_dir("child", s_base=sessions_base)
            self.assertEqual((meta_dir / "playground" / "later.txt").read_text(encoding="utf-8"), "late playground")

