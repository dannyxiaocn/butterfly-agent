from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ui.cli.visit import gather_room_data

from nutshell.runtime.ipc import FileIPC
from nutshell.session_engine.session_init import init_session


class FullSystemTest(unittest.TestCase):
    def test_end_to_end_bootstrap_and_room_view(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions_base = root / "sessions"
            system_base = root / "_sessions"
            entity_base = root / "entity"
            entity_dir = entity_base / "demo"
            meta_dir = sessions_base / "demo_meta"

            (entity_dir / "prompts").mkdir(parents=True)
            (entity_dir / "agent.yaml").write_text("name: demo\nprovider: anthropic\n", encoding="utf-8")
            (meta_dir / "core" / "tools").mkdir(parents=True)
            (meta_dir / "core" / "skills").mkdir(parents=True)
            (meta_dir / "core" / "memory").mkdir(parents=True)
            (meta_dir / "playground").mkdir(parents=True)
            for name, content in [("system.md", "sys"), ("heartbeat.md", "beat"), ("session.md", "sess"), ("memory.md", "memory")]:
                (meta_dir / "core" / name).write_text(content, encoding="utf-8")

            def fake_create_session_venv(session_dir: Path) -> Path:
                venv = session_dir / ".venv"
                venv.mkdir(parents=True, exist_ok=True)
                return venv

            with patch("nutshell.session_engine.session_init._create_session_venv", side_effect=fake_create_session_venv), patch(
                "nutshell.session_engine.session_init.ensure_meta_session",
                side_effect=lambda *args, **kwargs: meta_dir,
            ), patch("nutshell.session_engine.session_init._meta_is_synced", return_value=True), patch(
                "nutshell.session_engine.session_init.ensure_gene_initialized"
            ), patch(
                "nutshell.session_engine.session_init.start_meta_agent"
            ), patch("nutshell.session_engine.session_init.sync_from_entity"):
                init_session(
                    "demo-session",
                    "demo",
                    sessions_base=sessions_base,
                    system_sessions_base=system_base,
                    entity_base=entity_base,
                )

            ipc = FileIPC(system_base / "demo-session")
            msg_id = ipc.send_message("hello")
            ipc.append_context(
                {
                    "type": "turn",
                    "user_input_id": msg_id,
                    "messages": [{"role": "assistant", "content": "world"}],
                }
            )
            data = gather_room_data("demo-session", sessions_base=sessions_base, system_base=system_base)
        self.assertEqual(data["entity"], "demo")
        self.assertEqual(data["recent_context"][-1]["summary"], "world")



if __name__ == "__main__":
    unittest.main()
