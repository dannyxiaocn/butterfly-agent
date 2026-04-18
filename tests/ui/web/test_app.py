from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from butterfly.session_engine.task_cards import TaskCard, save_card
from ui.web.app import create_app
from butterfly.service.sessions_service import _is_stale_stopped


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
        json.dumps({"session_id": session_id, "agent": "agent", "created_at": "2026-01-01T00:00:00"}),
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

    def test_invalid_session_id_returns_400_instead_of_server_error(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app, raise_server_exceptions=False) as client:
                responses = [
                    client.get("/api/sessions/bad.id"),
                    client.get("/api/sessions/bad.id/history"),
                    client.get("/api/sessions/bad.id/hud"),
                    client.get("/api/sessions/bad.id/events"),
                    client.post("/api/sessions/bad.id/messages", json={"content": "hi"}),
                    client.post("/api/sessions", json={"id": "bad.id", "agent": "agent"}),
                ]

            for response in responses:
                self.assertEqual(response.status_code, 400)

    def test_history_endpoint_returns_display_history(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            system_dir = root / "_sessions" / "test-session"
            (system_dir / "context.jsonl").write_text(
                "\n".join([
                    json.dumps({"type": "user_input", "id": "u1", "content": "hello", "ts": "2026-03-25T10:00:00"}),
                    json.dumps({
                        "type": "turn",
                        "user_input_id": "u1",
                        "ts": "2026-03-25T10:00:05",
                        "messages": [{"role": "assistant", "content": "hi there"}],
                    }),
                ]) + "\n",
                encoding="utf-8",
            )
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.get("/api/sessions/test-session/history")

            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            self.assertEqual([event["type"] for event in payload["events"]], ["user", "agent"])
            self.assertEqual(payload["events"][0]["content"], "hello")
            self.assertEqual(payload["events"][1]["content"], "hi there")

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

    def test_get_tasks_returns_task_cards_with_new_schema(self) -> None:
        """GET /tasks returns task cards with new field names (description, last_finished_at)."""
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            tasks_dir = root / "sessions" / "test-session" / "core" / "tasks"
            save_card(tasks_dir, TaskCard(name="duty", description="check inbox", interval=300))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.get("/api/sessions/test-session/tasks")
            self.assertEqual(resp.status_code, 200)
            cards = resp.json()["cards"]
            duty = next(c for c in cards if c["name"] == "duty")
            self.assertEqual(duty["description"], "check inbox")
            self.assertEqual(duty["interval"], 300)

    def test_put_tasks_by_name_creates_named_card(self) -> None:
        """PUT /tasks with {name, description} should create/update the named card."""
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                put_resp = client.put(
                    "/api/sessions/test-session/tasks",
                    json={"name": "duty", "description": "check messages"},
                )
                self.assertEqual(put_resp.status_code, 200)
                get_resp = client.get("/api/sessions/test-session/tasks")
            cards = get_resp.json()["cards"]
            names = [c["name"] for c in cards]
            self.assertIn("duty", names)
            card = next(c for c in cards if c["name"] == "duty")
            self.assertEqual(card["description"], "check messages")

    def test_put_tasks_by_name_updates_existing_card_not_duplicates(self) -> None:
        """Saving a card by name overwrites it, does not create a second card."""
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                client.put("/api/sessions/test-session/tasks", json={"name": "duty", "description": "v1"})
                client.put("/api/sessions/test-session/tasks", json={"name": "duty", "description": "v2"})
                get_resp = client.get("/api/sessions/test-session/tasks")
            cards = get_resp.json()["cards"]
            duty_cards = [c for c in cards if c["name"] == "duty"]
            self.assertEqual(len(duty_cards), 1, "second PUT must update, not duplicate")
            self.assertEqual(duty_cards[0]["description"], "v2")

    def test_put_tasks_by_name_preserves_existing_metadata(self) -> None:
        """Editing an existing recurring card must not wipe its scheduling metadata."""
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            tasks_dir = root / "sessions" / "test-session" / "core" / "tasks"
            save_card(
                tasks_dir,
                TaskCard(
                    name="duty",
                    description="v1",
                    interval=600,
                    status="paused",
                    last_finished_at="2026-04-09T10:00:00",
                    created_at="2026-04-08T09:00:00",
                ),
            )
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                put_resp = client.put(
                    "/api/sessions/test-session/tasks",
                    json={"name": "duty", "description": "v2"},
                )
                self.assertEqual(put_resp.status_code, 200)
                get_resp = client.get("/api/sessions/test-session/tasks")

            card = next(c for c in get_resp.json()["cards"] if c["name"] == "duty")
            self.assertEqual(card["description"], "v2")
            self.assertEqual(card["interval"], 600)
            self.assertEqual(card["status"], "paused")
            self.assertEqual(card["last_finished_at"], "2026-04-09T10:00:00")
            self.assertEqual(card["created_at"], "2026-04-08T09:00:00")

    def test_put_tasks_by_name_updates_interval(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.put(
                    "/api/sessions/test-session/tasks",
                    json={
                        "name": "duty",
                        "description": "check messages",
                        "interval": 900,
                    },
                )
                self.assertEqual(resp.status_code, 200)
                cards = client.get("/api/sessions/test-session/tasks").json()["cards"]
            duty = next(c for c in cards if c["name"] == "duty")
            self.assertEqual(duty["interval"], 900)

    def test_put_tasks_can_rename_card(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                client.put("/api/sessions/test-session/tasks", json={"name": "followup", "description": "v1"})
                resp = client.put(
                    "/api/sessions/test-session/tasks",
                    json={"previous_name": "followup", "name": "followup-next", "description": "v2"},
                )
                self.assertEqual(resp.status_code, 200)
                cards = client.get("/api/sessions/test-session/tasks").json()["cards"]
            self.assertEqual({c["name"] for c in cards}, {"followup-next"})

    def test_put_tasks_rejects_invalid_renamed_card_name_without_deleting_original(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                client.put("/api/sessions/test-session/tasks", json={"name": "followup", "description": "v1"})
                resp = client.put(
                    "/api/sessions/test-session/tasks",
                    json={"previous_name": "followup", "name": "bad/name", "description": "v2"},
                )
                self.assertEqual(resp.status_code, 400)
                cards = client.get("/api/sessions/test-session/tasks").json()["cards"]
            self.assertEqual({c["name"] for c in cards}, {"followup"})
            self.assertEqual(cards[0]["description"], "v1")

    def test_put_tasks_rejects_invalid_schedule_window(self) -> None:
        """starts_at/ends_at validation still works for backward compat."""
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.put(
                    "/api/sessions/test-session/tasks",
                    json={
                        "name": "followup",
                        "description": "v1",
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
                client.put("/api/sessions/test-session/tasks", json={"name": "cleanup", "description": "do it"})
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

    def test_get_models_returns_provider_catalog(self) -> None:
        """GET /api/models exposes the provider → models matrix used by the web config editor."""
        with TemporaryDirectory() as td:
            root = Path(td)
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.get("/api/models")

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertIn("providers", payload)
        names = {p["provider"] for p in payload["providers"]}
        self.assertIn("anthropic", names)
        self.assertIn("openai", names)
        self.assertIn("kimi-coding-plan", names)
        self.assertIn("codex-oauth", names)
        for p in payload["providers"]:
            self.assertIn("default_model", p)
            self.assertTrue(p["default_model"], f"{p['provider']} has no default_model")

    def test_get_agents_lists_agenthub_entries(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "sessions").mkdir()
            (root / "_sessions").mkdir()
            agenthub = root / "agenthub"
            for name in ("agent", "custom"):
                (agenthub / name).mkdir(parents=True)
                (agenthub / name / "config.yaml").write_text("agent: " + name, encoding="utf-8")
            # A dir without config.yaml must be ignored.
            (agenthub / "incomplete").mkdir()
            app = create_app(root / "sessions", root / "_sessions", agenthub_dir=agenthub)
            with TestClient(app) as client:
                resp = client.get("/api/agents")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["agents"], ["agent", "custom"])

    def test_asset_md_round_trip(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                put_resp = client.put(
                    "/api/sessions/test-session/assets/tools",
                    json={"text": "bash\nweb_search_brave\n"},
                )
                self.assertEqual(put_resp.status_code, 200)
                get_resp = client.get("/api/sessions/test-session/assets/tools")
                self.assertEqual(get_resp.json()["text"], "bash\nweb_search_brave\n")
                # Unknown asset name is rejected.
                bad = client.get("/api/sessions/test-session/assets/passwords")
                self.assertEqual(bad.status_code, 400)

    def test_prompt_md_round_trip(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                put_resp = client.put(
                    "/api/sessions/test-session/prompts/system",
                    json={"text": "You are Butterfly."},
                )
                self.assertEqual(put_resp.status_code, 200)
                get_resp = client.get("/api/sessions/test-session/prompts/system")
                self.assertEqual(get_resp.json()["text"], "You are Butterfly.")
                # Only system/task/env are allowed.
                bad = client.get("/api/sessions/test-session/prompts/hidden")
                self.assertEqual(bad.status_code, 400)

    def test_legacy_name_field_migrates_to_agent(self) -> None:
        """Sessions saved before v2.0.19 carry `name: foo`; read_config must
        surface it as `agent` so the whitelist doesn't silently drop it."""
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            cfg = root / "sessions" / "test-session" / "core" / "config.yaml"
            cfg.write_text("name: legacy_agent\nmodel: claude-sonnet-4-6\n", encoding="utf-8")
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.get("/api/sessions/test-session/config")
                self.assertEqual(resp.status_code, 200)
                params = resp.json()["params"]
                self.assertEqual(params["agent"], "legacy_agent")
                self.assertNotIn("name", params)

    def test_put_config_json_drops_unknown_keys(self) -> None:
        """Same whitelist applies to the legacy JSON /config endpoint."""
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.put(
                    "/api/sessions/test-session/config",
                    json={"params": {"provider": "anthropic", "bogus_field": 42}},
                )
                self.assertEqual(resp.status_code, 200)
                self.assertNotIn("bogus_field", resp.json()["params"])

    # ── v2.0.19 per-file editor endpoints ────────────────────────────────────

    def test_list_agents_endpoint_surfaces_agenthub_entries(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.get("/api/agents")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertIn("agents", payload)
        self.assertIn("agent", payload["agents"])
        self.assertIn("butterfly_dev", payload["agents"])

    def test_get_and_put_asset_md_round_trip(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            core = root / "sessions" / "test-session" / "core"
            (core / "tools.md").write_text("bash\nread\n", encoding="utf-8")
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                get_resp = client.get("/api/sessions/test-session/assets/tools")
                self.assertEqual(get_resp.status_code, 200)
                self.assertEqual(get_resp.json()["text"], "bash\nread\n")

                put_resp = client.put(
                    "/api/sessions/test-session/assets/tools",
                    json={"text": "bash\nread\nglob\n"},
                )
                self.assertEqual(put_resp.status_code, 200)
                self.assertEqual(put_resp.json()["text"], "bash\nread\nglob\n")

                # Round-trip on disk
                self.assertEqual((core / "tools.md").read_text(encoding="utf-8"), "bash\nread\nglob\n")

    def test_put_asset_md_creates_file_if_missing(self) -> None:
        """Session directories are pre-created but tools.md / skills.md may not
        exist until the user clicks Save. The endpoint should create the
        file rather than 404'ing the write path."""
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                # Empty GET on missing file returns empty string, not 404.
                get_resp = client.get("/api/sessions/test-session/assets/skills")
                self.assertEqual(get_resp.status_code, 200)
                self.assertEqual(get_resp.json()["text"], "")

                put_resp = client.put(
                    "/api/sessions/test-session/assets/skills",
                    json={"text": "brave\n"},
                )
                self.assertEqual(put_resp.status_code, 200)
                skills_path = root / "sessions" / "test-session" / "core" / "skills.md"
                self.assertTrue(skills_path.exists())
                self.assertEqual(skills_path.read_text(encoding="utf-8"), "brave\n")

    def test_asset_md_rejects_unknown_asset_name(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                get_resp = client.get("/api/sessions/test-session/assets/evil")
                self.assertEqual(get_resp.status_code, 400)
                put_resp = client.put(
                    "/api/sessions/test-session/assets/evil",
                    json={"text": "x"},
                )
                self.assertEqual(put_resp.status_code, 400)

    def test_asset_md_put_requires_text_string(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                # Missing body field
                resp1 = client.put("/api/sessions/test-session/assets/tools", json={})
                # Non-string text
                resp2 = client.put("/api/sessions/test-session/assets/tools", json={"text": 42})
            self.assertEqual(resp1.status_code, 400)
            self.assertEqual(resp2.status_code, 400)

    def test_asset_md_on_missing_session_returns_404(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "sessions").mkdir()
            (root / "_sessions").mkdir()
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.get("/api/sessions/nonexistent/assets/tools")
            self.assertEqual(resp.status_code, 404)

    def test_get_and_put_prompt_md_round_trip(self) -> None:
        """prompts/{system,task,env} read/write against core/<name>.md (flat)."""
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            core = root / "sessions" / "test-session" / "core"
            (core / "system.md").write_text("You are a helpful agent.\n", encoding="utf-8")
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                get_resp = client.get("/api/sessions/test-session/prompts/system")
                self.assertEqual(get_resp.status_code, 200)
                self.assertEqual(get_resp.json()["text"], "You are a helpful agent.\n")

                put_resp = client.put(
                    "/api/sessions/test-session/prompts/task",
                    json={"text": "Your task is to test.\n"},
                )
                self.assertEqual(put_resp.status_code, 200)
                # v2.0.19: sessions store prompts flat under core/<name>.md,
                # not core/prompts/<name>.md as agenthub/ does.
                self.assertEqual((core / "task.md").read_text(encoding="utf-8"), "Your task is to test.\n")

    def test_prompt_md_rejects_unknown_prompt_name(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                get_resp = client.get("/api/sessions/test-session/prompts/evil")
                self.assertEqual(get_resp.status_code, 400)
                put_resp = client.put(
                    "/api/sessions/test-session/prompts/evil",
                    json={"text": "x"},
                )
                self.assertEqual(put_resp.status_code, 400)

    def test_create_session_same_second_does_not_silently_reuse_existing_id(self) -> None:
        fixed = datetime(2026, 4, 10, 23, 30, 0)
        with TemporaryDirectory() as td:
            root = Path(td)
            app = create_app(root / "sessions", root / "_sessions")
            with patch("ui.web.app.datetime") as mock_dt:
                mock_dt.now.return_value = fixed
                mock_dt.fromisoformat = datetime.fromisoformat
                with TestClient(app) as client:
                    first = client.post("/api/sessions", json={"agent": "agent"})
                    second = client.post("/api/sessions", json={"agent": "agent"})

            self.assertEqual(first.status_code, 200)
            self.assertTrue(
                second.status_code == 409 or first.json()["id"] != second.json()["id"],
                "session creation should either generate a unique ID or reject the duplicate",
            )
