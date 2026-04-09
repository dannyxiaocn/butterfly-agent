from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

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
