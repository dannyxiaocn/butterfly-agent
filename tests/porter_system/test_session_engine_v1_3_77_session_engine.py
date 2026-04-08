from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nutshell.core.agent import Agent
from nutshell.runtime.ipc import FileIPC
from nutshell.session_engine.entity_config import AgentConfig, _string_list
from nutshell.session_engine.session_init import _load_entity_params, init_session
from nutshell.session_engine.session_params import read_session_params, write_session_params
from nutshell.session_engine.session import Session
from nutshell.session_engine.session_status import ensure_session_status, read_session_status, write_session_status


class SessionEngineTest(unittest.TestCase):
    def test_string_list_normalizes_scalars_and_lists(self) -> None:
        self.assertEqual(_string_list(None), [])
        self.assertEqual(_string_list("one"), ["one"])
        self.assertEqual(_string_list(["one", 2, None]), ["one", "2"])

    def test_agent_config_reads_inheritance_metadata(self) -> None:
        with TemporaryDirectory() as tmp:
            entity_dir = Path(tmp) / "demo"
            entity_dir.mkdir()
            (entity_dir / "agent.yaml").write_text(
                "extends: agent\nlink: prompts\nown: memory\nappend:\n  - skills\n",
                encoding="utf-8",
            )
            config = AgentConfig.from_path(entity_dir)
        self.assertEqual(config.extends, "agent")
        self.assertEqual(config.inheritance.link, ["prompts"])
        self.assertEqual(config.inheritance.own, ["memory"])
        self.assertEqual(config.inheritance.append, ["skills"])

    def test_load_entity_params_converts_legacy_persistent_flag(self) -> None:
        with TemporaryDirectory() as tmp:
            entity_dir = Path(tmp) / "demo"
            entity_dir.mkdir()
            (entity_dir / "agent.yaml").write_text(
                "params:\n  persistent: true\n  default_task: keep working\n",
                encoding="utf-8",
            )
            params = _load_entity_params(entity_dir)
        self.assertEqual(params["session_type"], "persistent")
        self.assertEqual(params["default_task"], "keep working")

    def test_session_params_fall_back_on_corrupt_json(self) -> None:
        with TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "session"
            (session_dir / "core").mkdir(parents=True)
            (session_dir / "core" / "params.json").write_text("{not json", encoding="utf-8")
            params = read_session_params(session_dir)
        self.assertEqual(params["heartbeat_interval"], 600.0)

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
            entity_base = root / "entity"
            entity_dir = entity_base / "demo"
            meta_dir = sessions_base / "demo_meta"

            (entity_dir / "prompts").mkdir(parents=True)
            (entity_dir / "agent.yaml").write_text(
                "name: demo\nprovider: anthropic\nmodel: claude-sonnet-4-6\nparams:\n  heartbeat_interval: 42\n",
                encoding="utf-8",
            )

            (meta_dir / "core" / "tools").mkdir(parents=True)
            (meta_dir / "core" / "skills" / "alpha").mkdir(parents=True)
            (meta_dir / "core" / "memory").mkdir(parents=True)
            (meta_dir / "playground").mkdir(parents=True)
            (meta_dir / "core" / "system.md").write_text("sys", encoding="utf-8")
            (meta_dir / "core" / "heartbeat.md").write_text("beat", encoding="utf-8")
            (meta_dir / "core" / "session.md").write_text("sess", encoding="utf-8")
            (meta_dir / "core" / "memory.md").write_text("meta memory", encoding="utf-8")
            (meta_dir / "core" / "memory" / "layer.md").write_text("layer", encoding="utf-8")
            (meta_dir / "core" / "tools" / "bash.json").write_text(
                json.dumps({"name": "bash", "input_schema": {"type": "object"}}),
                encoding="utf-8",
            )
            (meta_dir / "core" / "skills" / "alpha" / "SKILL.md").write_text("# alpha", encoding="utf-8")
            (meta_dir / "playground" / "seed.txt").write_text("seed", encoding="utf-8")

            def fake_create_session_venv(session_dir: Path) -> Path:
                venv = session_dir / ".venv"
                venv.mkdir(parents=True, exist_ok=True)
                return venv

            with patch("nutshell.session_engine.session_init._create_session_venv", side_effect=fake_create_session_venv), patch(
                "nutshell.session_engine.session_init.ensure_meta_session",
                side_effect=lambda *args, **kwargs: meta_dir,
            ), patch("nutshell.session_engine.session_init._meta_is_synced", return_value=True), patch(
                "nutshell.session_engine.session_init.check_meta_alignment"
            ), patch("nutshell.session_engine.session_init.ensure_gene_initialized"), patch(
                "nutshell.session_engine.session_init.start_meta_agent"
            ), patch("nutshell.session_engine.session_init.sync_from_entity"):
                init_session(
                    "s1",
                    "demo",
                    sessions_base=sessions_base,
                    system_sessions_base=system_base,
                    entity_base=entity_base,
                )

            core_dir = sessions_base / "s1" / "core"
            self.assertEqual((core_dir / "system.md").read_text(encoding="utf-8"), "sys")
            self.assertEqual((core_dir / "memory.md").read_text(encoding="utf-8"), "meta memory")
            self.assertTrue((core_dir / "memory" / "layer.md").exists())
            self.assertTrue((core_dir / "tools" / "bash.json").exists())
            self.assertTrue((core_dir / "skills" / "alpha" / "SKILL.md").exists())
            self.assertTrue((sessions_base / "s1" / "playground" / "seed.txt").exists())
            params = read_session_params(sessions_base / "s1")
            self.assertEqual(params["heartbeat_interval"], 42)

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

            with patch("nutshell.session_engine.session.asyncio.sleep", side_effect=_fake_sleep):
                asyncio.run(session.run_daemon_loop(ipc, stop_event=stop_event))

            status = read_session_status(session.system_dir)
            self.assertEqual(status["status"], "active")


if __name__ == "__main__":
    unittest.main()
