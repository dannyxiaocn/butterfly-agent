from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from nutshell.session_engine.session_params import read_session_params, write_session_params
from nutshell.session_engine.task_cards import TaskCard, save_card
from ui.web.app import create_app
from ui.web.sessions import _is_stale_stopped


def _make_session(root: Path, session_id: str = "test-session") -> Path:
    """Create a minimal session directory structure for web tests."""
    sessions_dir = root / "sessions"
    system_dir = root / "_sessions" / session_id
    core_dir = sessions_dir / session_id / "core"
    tasks_dir = core_dir / "tasks"
    core_dir.mkdir(parents=True)
    tasks_dir.mkdir()
    system_dir.mkdir(parents=True)
    (system_dir / "context.jsonl").touch()
    (system_dir / "events.jsonl").touch()
    (system_dir / "manifest.json").write_text(
        json.dumps({"session_id": session_id, "entity": "agent", "created_at": "2026-01-01T00:00:00"}),
        encoding="utf-8",
    )
    return root


class WebUnitTests(unittest.TestCase):
    def test_stale_stopped_handles_timezone_aware_timestamp(self) -> None:
        result = _is_stale_stopped({"status": "stopped", "stopped_at": "2026-04-01T00:00:00+00:00"})
        self.assertIsInstance(result, bool)

    def test_stop_and_start_missing_session_return_404_without_creating_state(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                stop_response = client.post("/api/sessions/missing/stop")
                start_response = client.post("/api/sessions/missing/start")

            self.assertEqual(stop_response.status_code, 404)
            self.assertEqual(start_response.status_code, 404)
            self.assertFalse((root / "_sessions" / "missing").exists())

    def test_get_tasks_returns_empty_cards_for_new_session(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.get("/api/sessions/test-session/tasks")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertIn("cards", data)
            self.assertIsInstance(data["cards"], list)

    def test_get_tasks_migrates_legacy_tasks_md(self) -> None:
        """GET /tasks must migrate tasks.md → tasks/ on first access."""
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            tasks_md = root / "sessions" / "test-session" / "core" / "tasks.md"
            tasks_md.write_text("legacy task content", encoding="utf-8")
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.get("/api/sessions/test-session/tasks")
            self.assertEqual(resp.status_code, 200)
            self.assertFalse(tasks_md.exists(), "tasks.md should be removed after migration")
            cards = resp.json()["cards"]
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0]["content"], "legacy task content")

    def test_get_tasks_migrates_legacy_default_task_into_heartbeat_card(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            write_session_params(
                root / "sessions" / "test-session",
                session_type="persistent",
                default_task="check inbox",
                heartbeat_interval=300,
            )
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.get("/api/sessions/test-session/tasks")
            self.assertEqual(resp.status_code, 200)
            cards = resp.json()["cards"]
            heartbeat = next(c for c in cards if c["name"] == "heartbeat")
            self.assertEqual(heartbeat["content"], "check inbox")
            self.assertEqual(heartbeat["interval"], 300)
            self.assertIsNone(read_session_params(root / "sessions" / "test-session")["default_task"])

    def test_put_tasks_by_name_creates_named_card(self) -> None:
        """PUT /tasks with {name, content} should create/update the named card."""
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                put_resp = client.put(
                    "/api/sessions/test-session/tasks",
                    json={"name": "heartbeat", "content": "check messages"},
                )
                self.assertEqual(put_resp.status_code, 200)
                get_resp = client.get("/api/sessions/test-session/tasks")
            cards = get_resp.json()["cards"]
            names = [c["name"] for c in cards]
            self.assertIn("heartbeat", names)
            card = next(c for c in cards if c["name"] == "heartbeat")
            self.assertEqual(card["content"], "check messages")

    def test_put_tasks_by_name_updates_existing_card_not_duplicates(self) -> None:
        """Saving a card by name overwrites it, does not create a second card."""
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                client.put("/api/sessions/test-session/tasks", json={"name": "heartbeat", "content": "v1"})
                client.put("/api/sessions/test-session/tasks", json={"name": "heartbeat", "content": "v2"})
                get_resp = client.get("/api/sessions/test-session/tasks")
            cards = get_resp.json()["cards"]
            heartbeat_cards = [c for c in cards if c["name"] == "heartbeat"]
            self.assertEqual(len(heartbeat_cards), 1, "second PUT must update, not duplicate")
            self.assertEqual(heartbeat_cards[0]["content"], "v2")

    def test_put_tasks_by_name_preserves_existing_metadata(self) -> None:
        """Editing an existing recurring card must not wipe its scheduling metadata."""
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            tasks_dir = root / "sessions" / "test-session" / "core" / "tasks"
            save_card(
                tasks_dir,
                TaskCard(
                    name="heartbeat",
                    content="v1",
                    interval=600,
                    status="paused",
                    last_run_at="2026-04-09T10:00:00",
                    created_at="2026-04-08T09:00:00",
                ),
            )
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                put_resp = client.put(
                    "/api/sessions/test-session/tasks",
                    json={"name": "heartbeat", "content": "v2"},
                )
                self.assertEqual(put_resp.status_code, 200)
                get_resp = client.get("/api/sessions/test-session/tasks")

            card = next(c for c in get_resp.json()["cards"] if c["name"] == "heartbeat")
            self.assertEqual(card["content"], "v2")
            self.assertEqual(card["interval"], 600)
            self.assertEqual(card["status"], "paused")
            self.assertEqual(card["last_run_at"], "2026-04-09T10:00:00")
            self.assertEqual(card["created_at"], "2026-04-08T09:00:00")

    def test_put_tasks_by_name_updates_schedule_fields_and_syncs_heartbeat_interval(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.put(
                    "/api/sessions/test-session/tasks",
                    json={
                        "name": "heartbeat",
                        "content": "check messages",
                        "interval": 900,
                        "starts_at": "2026-04-10T09:00:00",
                        "ends_at": "2026-04-10T18:00:00",
                    },
                )
                self.assertEqual(resp.status_code, 200)
                cards = client.get("/api/sessions/test-session/tasks").json()["cards"]
            heartbeat = next(c for c in cards if c["name"] == "heartbeat")
            self.assertEqual(heartbeat["interval"], 900)
            self.assertEqual(heartbeat["starts_at"], "2026-04-10T09:00:00")
            self.assertEqual(heartbeat["ends_at"], "2026-04-10T18:00:00")
            self.assertEqual(read_session_params(root / "sessions" / "test-session")["heartbeat_interval"], 900)

    def test_put_tasks_can_rename_card(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                client.put("/api/sessions/test-session/tasks", json={"name": "followup", "content": "v1"})
                resp = client.put(
                    "/api/sessions/test-session/tasks",
                    json={"previous_name": "followup", "name": "followup-next", "content": "v2"},
                )
                self.assertEqual(resp.status_code, 200)
                cards = client.get("/api/sessions/test-session/tasks").json()["cards"]
            self.assertEqual({c["name"] for c in cards}, {"followup-next"})

    def test_put_tasks_rejects_invalid_renamed_card_name_without_deleting_original(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                client.put("/api/sessions/test-session/tasks", json={"name": "followup", "content": "v1"})
                resp = client.put(
                    "/api/sessions/test-session/tasks",
                    json={"previous_name": "followup", "name": "bad/name", "content": "v2"},
                )
                self.assertEqual(resp.status_code, 400)
                cards = client.get("/api/sessions/test-session/tasks").json()["cards"]
            self.assertEqual({c["name"] for c in cards}, {"followup"})
            self.assertEqual(cards[0]["content"], "v1")

    def test_put_tasks_rejects_invalid_schedule_window(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.put(
                    "/api/sessions/test-session/tasks",
                    json={
                        "name": "followup",
                        "content": "v1",
                        "starts_at": "2026-04-10T18:00:00",
                        "ends_at": "2026-04-10T09:00:00",
                    },
                )
            self.assertEqual(resp.status_code, 400)

    def test_delete_task_removes_card(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                client.put("/api/sessions/test-session/tasks", json={"name": "cleanup", "content": "do it"})
                delete_resp = client.delete("/api/sessions/test-session/tasks/cleanup")
                self.assertEqual(delete_resp.status_code, 200)
                cards = client.get("/api/sessions/test-session/tasks").json()["cards"]
            self.assertEqual(cards, [])

    def test_delete_task_rejects_invalid_name(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.delete("/api/sessions/test-session/tasks/bad%5Cname")
            self.assertEqual(resp.status_code, 400)

    def test_create_session_same_second_does_not_silently_reuse_existing_id(self) -> None:
        fixed = datetime(2026, 4, 10, 23, 30, 0)
        with TemporaryDirectory() as td:
            root = Path(td)
            app = create_app(root / "sessions", root / "_sessions")
            with patch("ui.web.app.datetime") as mock_dt:
                mock_dt.now.return_value = fixed
                mock_dt.fromisoformat = datetime.fromisoformat
                with TestClient(app) as client:
                    first = client.post("/api/sessions", json={"entity": "agent"})
                    second = client.post("/api/sessions", json={"entity": "agent"})

            self.assertEqual(first.status_code, 200)
            self.assertTrue(
                second.status_code == 409 or first.json()["id"] != second.json()["id"],
                "session creation should either generate a unique ID or reject the duplicate",
            )
