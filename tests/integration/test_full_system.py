from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from butterfly.runtime.ipc import FileIPC
from butterfly.session_engine.session_init import init_session


class FullSystemTest(unittest.TestCase):
    def test_end_to_end_bootstrap(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions_base = root / "sessions"
            system_base = root / "_sessions"
            agent_base = root / "agenthub"
            agent_dir = agent_base / "demo"
            meta_dir = sessions_base / "demo_meta"

            (agent_dir / "prompts").mkdir(parents=True)
            (agent_dir / "config.yaml").write_text("name: demo\nprovider: anthropic\n", encoding="utf-8")
            (meta_dir / "core" / "tools").mkdir(parents=True)
            (meta_dir / "core" / "skills").mkdir(parents=True)
            (meta_dir / "core" / "memory").mkdir(parents=True)
            (meta_dir / "playground").mkdir(parents=True)
            for name, content in [("system.md", "sys"), ("task.md", "task"), ("env.md", "env"), ("memory.md", "memory"), ("config.yaml", "name: demo\n")]:
                (meta_dir / "core" / name).write_text(content, encoding="utf-8")

            def fake_create_session_venv(session_dir: Path) -> Path:
                venv = session_dir / ".venv"
                venv.mkdir(parents=True, exist_ok=True)
                return venv

            with patch("butterfly.session_engine.session_init._create_session_venv", side_effect=fake_create_session_venv), patch(
                "butterfly.session_engine.session_init.ensure_meta_session",
                side_effect=lambda *args, **kwargs: meta_dir,
            ), patch(
                "butterfly.session_engine.session_init.ensure_gene_initialized"
            ), patch(
                "butterfly.session_engine.session_init.start_meta_agent"
            ), patch("butterfly.session_engine.session_init.sync_from_agent"):
                init_session(
                    "demo-session",
                    "demo",
                    sessions_base=sessions_base,
                    system_sessions_base=system_base,
                    agent_base=agent_base,
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
            # Verify session was created and IPC works
            self.assertTrue((system_base / "demo-session").exists())
            context_path = system_base / "demo-session" / "context.jsonl"
            self.assertTrue(context_path.exists())



if __name__ == "__main__":
    unittest.main()
