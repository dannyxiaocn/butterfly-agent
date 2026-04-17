from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from butterfly.core.agent import Agent
from butterfly.runtime.ipc import FileIPC
from butterfly.session_engine.agent_config import AgentConfig
from butterfly.session_engine.session_init import init_session
from butterfly.session_engine.session_config import read_config, write_config
from butterfly.session_engine.session import Session
from butterfly.session_engine.session_status import ensure_session_status, read_session_status, write_session_status


class SessionEngineTest(unittest.TestCase):
    def test_agent_config_reads_manifest_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / "demo"
            agent_dir.mkdir()
            (agent_dir / "config.yaml").write_text(
                "name: demo\nmodel: claude-sonnet-4-6\nprovider: anthropic\n",
                encoding="utf-8",
            )
            config = AgentConfig.from_path(agent_dir)
        self.assertEqual(config.manifest["name"], "demo")
        self.assertEqual(config.manifest["model"], "claude-sonnet-4-6")

    def test_agent_config_requires_config_yaml(self) -> None:
        """AgentConfig raises FileNotFoundError when config.yaml is absent."""
        with TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / "demo"
            agent_dir.mkdir()
            with self.assertRaises(FileNotFoundError):
                AgentConfig.from_path(agent_dir)

    def test_session_config_read_write_roundtrip(self) -> None:
        with TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "session"
            (session_dir / "core").mkdir(parents=True)
            write_config(session_dir, model="gpt-4", thinking=True)
            cfg = read_config(session_dir)
        self.assertEqual(cfg["model"], "gpt-4")
        self.assertTrue(cfg["thinking"])

    def test_session_config_returns_defaults_when_absent(self) -> None:
        """read_config returns defaults when config.yaml is absent."""
        with TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "session"
            (session_dir / "core").mkdir(parents=True)
            cfg = read_config(session_dir)
        self.assertIsNone(cfg["model"])

    def test_session_status_round_trips_updates(self) -> None:
        with TemporaryDirectory() as tmp:
            system_dir = Path(tmp) / "_sessions" / "demo"
            ensure_session_status(system_dir)
            write_session_status(system_dir, status="stopped", pid=123)
            status = read_session_status(system_dir)
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(status["pid"], 123)
        self.assertIsNotNone(status["updated_at"])

    def test_init_session_copies_meta_seed_content(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions_base = root / "sessions"
            system_base = root / "_sessions"
            agent_base = root / "agenthub"
            agent_dir = agent_base / "demo"
            meta_dir = sessions_base / "demo_meta"

            (agent_dir / "prompts").mkdir(parents=True)
            (agent_dir / "config.yaml").write_text(
                "name: demo\nprovider: anthropic\nmodel: claude-sonnet-4-6\n",
                encoding="utf-8",
            )
            (agent_dir / "prompts" / "system.md").write_text("sys", encoding="utf-8")
            (agent_dir / "prompts" / "task.md").write_text("task", encoding="utf-8")
            (agent_dir / "prompts" / "env.md").write_text("env", encoding="utf-8")

            (meta_dir / "core" / "memory").mkdir(parents=True)
            (meta_dir / "playground").mkdir(parents=True)
            (meta_dir / "core" / "system.md").write_text("sys", encoding="utf-8")
            (meta_dir / "core" / "task.md").write_text("task", encoding="utf-8")
            (meta_dir / "core" / "env.md").write_text("env", encoding="utf-8")
            (meta_dir / "core" / "config.yaml").write_text(
                "name: demo\nmodel: claude-sonnet-4-6\n", encoding="utf-8"
            )
            (meta_dir / "core" / "memory.md").write_text("meta memory", encoding="utf-8")
            (meta_dir / "core" / "memory" / "layer.md").write_text("layer", encoding="utf-8")
            (meta_dir / "core" / "tools.md").write_text("bash\n", encoding="utf-8")
            (meta_dir / "core" / "skills.md").write_text("butterfly\n", encoding="utf-8")
            (meta_dir / "playground" / "seed.txt").write_text("seed", encoding="utf-8")

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
                    "s1",
                    "demo",
                    sessions_base=sessions_base,
                    system_sessions_base=system_base,
                    agent_base=agent_base,
                )

            core_dir = sessions_base / "s1" / "core"
            self.assertEqual((core_dir / "system.md").read_text(encoding="utf-8"), "sys")
            self.assertEqual((core_dir / "task.md").read_text(encoding="utf-8"), "task")
            self.assertEqual((core_dir / "env.md").read_text(encoding="utf-8"), "env")
            self.assertEqual((core_dir / "memory.md").read_text(encoding="utf-8"), "meta memory")
            self.assertTrue((core_dir / "memory" / "layer.md").exists())
            self.assertTrue((core_dir / "tools.md").exists())
            self.assertTrue((core_dir / "skills.md").exists())
            self.assertTrue((sessions_base / "s1" / "playground" / "seed.txt").exists())
            # Config should be copied
            self.assertTrue((core_dir / "config.yaml").exists())

    def test_run_daemon_loop_auto_expires_timezone_aware_stopped_session(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = Session(
                Agent(provider=None),
                session_id="demo",
                base_dir=root / "sessions",
                system_base=root / "_sessions",
            )
            ensure_session_status(session.system_dir)
            write_session_status(
                session.system_dir,
                status="stopped",
                stopped_at=(datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(),
            )
            ipc = FileIPC(session.system_dir)
            stop_event = asyncio.Event()

            async def _fake_sleep(_seconds: float) -> None:
                stop_event.set()

            with patch("butterfly.session_engine.session.asyncio.sleep", side_effect=_fake_sleep):
                asyncio.run(session.run_daemon_loop(ipc, stop_event=stop_event))

            status = read_session_status(session.system_dir)
            self.assertEqual(status["status"], "active")

    def test_session_default_id_uses_uuid_suffix_for_same_second_uniqueness(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixed = datetime(2026, 4, 10, 23, 59, 59)
            with patch("butterfly.session_engine.session.datetime") as mock_dt, patch(
                "butterfly.session_engine.session.uuid.uuid4"
            ) as mock_uuid:
                mock_dt.now.return_value = fixed
                mock_uuid.side_effect = [
                    SimpleNamespace(hex="aaaabbbbccccdddd"),
                    SimpleNamespace(hex="1111222233334444"),
                ]
                s1 = Session(Agent(provider=None), base_dir=root / "sessions", system_base=root / "_sessions")
                s2 = Session(Agent(provider=None), base_dir=root / "sessions", system_base=root / "_sessions")

            self.assertNotEqual(s1.session_dir.name, s2.session_dir.name)
            self.assertTrue(s1.session_dir.name.startswith("2026-04-10_23-59-59-"))
            self.assertTrue(s2.session_dir.name.startswith("2026-04-10_23-59-59-"))


if __name__ == "__main__":
    unittest.main()
