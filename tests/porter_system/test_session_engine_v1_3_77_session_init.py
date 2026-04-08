from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nutshell.session_engine.session_init import init_session


def _repo_root() -> Path:
    current = Path(__file__).resolve()
    for candidate in (current.parent, *current.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    raise RuntimeError("repo root not found")


class SessionInitUnitTests(unittest.TestCase):
    def test_init_session_stays_inside_custom_bases(self) -> None:
        unique_entity = f"unit_test_entity_{uuid.uuid4().hex}"
        leaked_meta_dir = _repo_root() / "sessions" / f"{unique_entity}_meta"
        try:
            with TemporaryDirectory() as td, patch(
                "nutshell.session_engine.entity_state._create_meta_venv",
                side_effect=lambda p: p / ".venv",
            ), patch(
                "nutshell.session_engine.session_init._create_session_venv",
                side_effect=lambda p: p / ".venv",
            ), patch("nutshell.session_engine.entity_state.start_meta_agent"):
                root = Path(td)
                entity_base = root / "entity"
                sessions_base = root / "sessions"
                system_base = root / "_sessions"
                entity_dir = entity_base / unique_entity
                (entity_dir / "prompts").mkdir(parents=True)
                (entity_dir / "tools").mkdir()
                (entity_dir / "skills").mkdir()
                (entity_dir / "prompts" / "system.md").write_text("system", encoding="utf-8")
                (entity_dir / "prompts" / "heartbeat.md").write_text("heartbeat", encoding="utf-8")
                (entity_dir / "prompts" / "session.md").write_text("session", encoding="utf-8")
                (entity_dir / "agent.yaml").write_text(
                    "\n".join(
                        [
                            "prompts:",
                            "  system: prompts/system.md",
                            "  heartbeat: prompts/heartbeat.md",
                            "  session_context: prompts/session.md",
                            "provider: anthropic",
                            "model: demo",
                        ]
                    ),
                    encoding="utf-8",
                )

                init_session(
                    session_id="demo",
                    entity_name=unique_entity,
                    sessions_base=sessions_base,
                    system_sessions_base=system_base,
                    entity_base=entity_base,
                )

                self.assertTrue((sessions_base / "demo").exists())
                self.assertTrue((sessions_base / f"{unique_entity}_meta").exists())
                self.assertFalse(leaked_meta_dir.exists())
        finally:
            if leaked_meta_dir.exists():
                shutil.rmtree(leaked_meta_dir)

