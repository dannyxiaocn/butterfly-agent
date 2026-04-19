from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from ui.web.app import _sse_format, create_app
from butterfly.service.sessions_service import create_session, sort_sessions
from ui.web.weixin import WeixinBridge


class WebHelpersTest(unittest.TestCase):
    def test_sse_format_includes_sequence_id(self) -> None:
        payload = _sse_format({"type": "agent", "content": "hello"}, seq=7)
        self.assertIn("id: 7", payload)
        self.assertIn("event: agent", payload)

    def test_sse_format_embeds_resume_offsets(self) -> None:
        payload = _sse_format({"type": "agent", "content": "hello"}, seq=7, ctx=12, evt=34)
        data_line = next(line for line in payload.splitlines() if line.startswith("data: "))
        parsed = json.loads(data_line.removeprefix("data: "))

        self.assertEqual(parsed["_ctx"], 12)
        self.assertEqual(parsed["_evt"], 34)
        self.assertEqual(parsed["content"], "hello")

    def test_sort_sessions_prefers_last_run_at_over_created_at(self) -> None:
        # v2.0.24: pure recency sort. A session whose last run is newer wins
        # regardless of state — the old _session_priority pin is gone.
        sessions = [
            {"id": "recent_run", "pid_alive": False, "status": "active",
             "model_state": "idle", "created_at": "2026-01-01T00:00:00",
             "last_run_at": "2026-04-18T12:00:00"},
            {"id": "fresh_created", "pid_alive": False, "status": "active",
             "model_state": "idle", "created_at": "2026-04-18T09:00:00"},
        ]
        ordered = sort_sessions(sessions)
        self.assertEqual(ordered[0]["id"], "recent_run")

    def test_sort_sessions_does_not_pin_stopped_below_active(self) -> None:
        # v2.0.24: a more recently active STOPPED session now sorts above an
        # older active-but-idle one — the prior _session_priority pin that
        # floated non-stopped sessions to the top was intentionally removed.
        sessions = [
            {"id": "stopped_recent", "pid_alive": False, "status": "stopped",
             "model_state": "idle", "created_at": "2026-01-01T00:00:00",
             "last_run_at": "2026-04-18T12:00:00"},
            {"id": "active_stale", "pid_alive": False, "status": "active",
             "model_state": "idle", "created_at": "2026-01-01T00:00:00",
             "last_run_at": "2026-04-10T09:00:00"},
        ]
        ordered = sort_sessions(sessions)
        self.assertEqual(ordered[0]["id"], "stopped_recent")

    def test_create_session_resolves_agent_name_from_relative_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions_dir = root / "sessions"
            system_dir = root / "_sessions"
            with patch("butterfly.session_engine.session_init.init_session") as init_mock:
                create_session("demo", "agenthub/agent", sessions_dir=sessions_dir, system_sessions_dir=system_dir)
        kwargs = init_mock.call_args.kwargs
        self.assertEqual(kwargs["agent_name"], "agent")

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
                json.dumps({"agent": "agent", "created_at": "2026-01-01T00:00:00"}),
                encoding="utf-8",
            )
            (system_sessions_dir / "agent_meta" / "manifest.json").write_text(
                json.dumps({"agent": "agent", "created_at": "2026-01-01T00:00:00"}),
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

    def test_weixin_new_command_generates_unique_session_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = WeixinBridge(root / "sessions", root / "_sessions")
            fixed = datetime(2026, 4, 10, 23, 59, 59)
            fake_client = object()

            async def _run() -> None:
                with patch("butterfly.service.sessions_service.create_session") as init_mock, patch.object(
                    bridge,
                    "_send_text",
                    new=AsyncMock(),
                ) as send_mock, patch("ui.web.weixin.datetime") as mock_dt, patch("ui.web.weixin.uuid.uuid4") as mock_uuid:
                    mock_dt.now.return_value = fixed
                    mock_uuid.side_effect = [
                        SimpleNamespace(hex="aaaabbbbccccdddd"),
                        SimpleNamespace(hex="1111222233334444"),
                    ]
                    await bridge._handle_command(fake_client, "user-1", "/new", None)
                    first_sid = bridge._current_session
                    await bridge._handle_command(fake_client, "user-1", "/new", None)
                    second_sid = bridge._current_session

                self.assertNotEqual(first_sid, second_sid)
                self.assertTrue(str(first_sid).startswith("2026-04-10_23-59-59-"))
                self.assertTrue(str(second_sid).startswith("2026-04-10_23-59-59-"))
                self.assertEqual(init_mock.call_count, 2)
                self.assertEqual(send_mock.await_count, 2)

            import asyncio
            asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
