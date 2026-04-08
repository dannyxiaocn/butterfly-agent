from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from ui.web.app import _sse_format, create_app
from ui.web.sessions import _init_session, _sort_sessions


class WebHelpersTest(unittest.TestCase):
    def test_sse_format_includes_sequence_id(self) -> None:
        payload = _sse_format({"type": "agent", "content": "hello"}, seq=7)
        self.assertIn("id: 7", payload)
        self.assertIn("event: agent", payload)

    def test_sort_sessions_prioritizes_running_before_idle(self) -> None:
        sessions = [
            {"id": "idle", "pid_alive": False, "status": "active", "model_state": "idle", "created_at": "2026-01-01T00:00:00"},
            {"id": "run", "pid_alive": True, "status": "active", "model_state": "running", "created_at": "2026-01-01T00:00:01"},
        ]
        ordered = _sort_sessions(sessions)
        self.assertEqual(ordered[0]["id"], "run")

    def test_init_session_resolves_entity_name_from_relative_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions_dir = root / "sessions"
            system_dir = root / "_sessions"
            with patch("nutshell.session_engine.session_init.init_session") as init_mock:
                _init_session(sessions_dir, system_dir, "demo", "entity/agent", 30.0)
        kwargs = init_mock.call_args.kwargs
        self.assertEqual(kwargs["entity_name"], "agent")
        self.assertEqual(kwargs["heartbeat"], 30.0)

    def test_create_app_lists_seeded_sessions_and_blocks_meta_chat(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions_dir = root / "sessions"
            system_sessions_dir = root / "_sessions"
            (sessions_dir / "demo" / "core").mkdir(parents=True)
            (sessions_dir / "agent_meta" / "core").mkdir(parents=True)
            (system_sessions_dir / "demo").mkdir(parents=True)
            (system_sessions_dir / "agent_meta").mkdir(parents=True)
            (system_sessions_dir / "demo" / "manifest.json").write_text(
                json.dumps({"entity": "agent", "created_at": "2026-01-01T00:00:00"}),
                encoding="utf-8",
            )
            (system_sessions_dir / "agent_meta" / "manifest.json").write_text(
                json.dumps({"entity": "agent", "created_at": "2026-01-01T00:00:00"}),
                encoding="utf-8",
            )
            (system_sessions_dir / "demo" / "status.json").write_text(json.dumps({"status": "active"}), encoding="utf-8")
            (system_sessions_dir / "agent_meta" / "status.json").write_text(json.dumps({"status": "active"}), encoding="utf-8")
            with patch("ui.web.weixin.WeixinBridge.start"), patch("ui.web.weixin.WeixinBridge.stop"):
                client = TestClient(create_app(sessions_dir, system_sessions_dir))
                sessions = client.get("/api/sessions")
                blocked = client.post("/api/sessions/agent_meta/messages", json={"content": "hi"})
        self.assertEqual(sessions.status_code, 200)
        self.assertEqual(len(sessions.json()), 2)
        self.assertEqual(blocked.status_code, 403)


if __name__ == "__main__":
    unittest.main()
