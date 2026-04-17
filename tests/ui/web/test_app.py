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
        self.assertIn("thinking_efforts", payload)
        names = {p["provider"] for p in payload["providers"]}
        # The 4 CLI-supported providers (plus openai-responses) must all be listed.
        self.assertIn("anthropic", names)
        self.assertIn("openai", names)
        self.assertIn("kimi-coding-plan", names)
        self.assertIn("codex-oauth", names)
        for p in payload["providers"]:
            self.assertIn("models", p)
            self.assertIsInstance(p["models"], list)
            self.assertTrue(p["models"], f"{p['provider']} has no models")
            self.assertIn(p["default_model"], p["models"])

    def test_get_config_yaml_returns_raw_yaml_text(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            # Seed a config.yaml with a recognisable marker.
            cfg_path = root / "sessions" / "test-session" / "core" / "config.yaml"
            cfg_path.write_text("name: demo\nmodel: gpt-5.4\nprovider: codex-oauth\n", encoding="utf-8")
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.get("/api/sessions/test-session/config/yaml")

        self.assertEqual(resp.status_code, 200)
        text = resp.json()["yaml"]
        self.assertIn("name: demo", text)
        self.assertIn("model: gpt-5.4", text)

    def test_put_config_yaml_round_trips(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                put_resp = client.put(
                    "/api/sessions/test-session/config/yaml",
                    json={"yaml": "name: x\nmodel: claude-sonnet-4-6\nprovider: anthropic\n"},
                )
                self.assertEqual(put_resp.status_code, 200)
                saved = put_resp.json()["params"]
                self.assertEqual(saved["model"], "claude-sonnet-4-6")
                self.assertEqual(saved["provider"], "anthropic")
                # YAML round-trip: subsequent GET surfaces the written fields.
                get_resp = client.get("/api/sessions/test-session/config/yaml")
                self.assertEqual(get_resp.status_code, 200)
                self.assertIn("claude-sonnet-4-6", get_resp.json()["yaml"])

    def test_put_config_yaml_rejects_malformed_yaml(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.put(
                    "/api/sessions/test-session/config/yaml",
                    json={"yaml": "name: [unterminated"},
                )
            self.assertEqual(resp.status_code, 400)

    def test_put_config_yaml_rejects_non_mapping(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.put(
                    "/api/sessions/test-session/config/yaml",
                    json={"yaml": "- just\n- a\n- list\n"},
                )
            self.assertEqual(resp.status_code, 400)

    def test_put_config_yaml_drops_unknown_keys(self) -> None:
        """v2.0.9 review fix: YAML PUT must whitelist-filter against DEFAULT_CONFIG.

        Previously the body was forwarded verbatim into write_config, so a
        client could persist arbitrary keys that then round-tripped via
        read_config's merge-over-defaults path.
        """
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                put_resp = client.put(
                    "/api/sessions/test-session/config/yaml",
                    json={
                        "yaml": (
                            "model: claude-sonnet-4-6\n"
                            "provider: anthropic\n"
                            "malicious_key: injected\n"
                            "__proto__: nope\n"
                        )
                    },
                )
                self.assertEqual(put_resp.status_code, 200)
                saved = put_resp.json()["params"]
                self.assertNotIn("malicious_key", saved)
                self.assertNotIn("__proto__", saved)
                # Subsequent GET surfaces the same filtered view.
                get_resp = client.get("/api/sessions/test-session/config/yaml")
                self.assertNotIn("malicious_key", get_resp.json()["yaml"])

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

    def test_json_and_yaml_put_coexist_without_stepping_on_each_other(self) -> None:
        """Alternating JSON and YAML PUTs must each preserve the other's writes."""
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                r1 = client.put(
                    "/api/sessions/test-session/config",
                    json={"params": {"provider": "anthropic", "model": "claude-sonnet-4-6"}},
                )
                self.assertEqual(r1.status_code, 200)
                r2 = client.put(
                    "/api/sessions/test-session/config/yaml",
                    json={"yaml": "name: renamed\n"},
                )
                self.assertEqual(r2.status_code, 200)
                saved = r2.json()["params"]
                # Write 2 (yaml) updated 'name' but must not wipe Write 1's model.
                self.assertEqual(saved["name"], "renamed")
                self.assertEqual(saved["model"], "claude-sonnet-4-6")
                self.assertEqual(saved["provider"], "anthropic")

    def test_models_catalog_default_model_is_in_supported_list(self) -> None:
        """Catalog invariant: every provider's default_model must appear in its
        models list AND its supported_efforts must be consistent with the
        thinking_style (effort-style providers have a non-empty list;
        budget/none-style providers have an empty list).
        """
        with TemporaryDirectory() as td:
            root = Path(td)
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.get("/api/models")
            payload = resp.json()
            for p in payload["providers"]:
                self.assertIn(p["default_model"], p["models"], p["provider"])
                style = p.get("thinking_style")
                supported = p.get("supported_efforts", [])
                if style == "effort":
                    self.assertTrue(supported, f"{p['provider']} effort-style but empty supported_efforts")
                else:
                    self.assertEqual(supported, [], f"{p['provider']} non-effort style must have empty supported_efforts")
            # xhigh is codex-only.
            codex = next(p for p in payload["providers"] if p["provider"] == "codex-oauth")
            responses = next(p for p in payload["providers"] if p["provider"] == "openai-responses")
            self.assertIn("xhigh", codex["supported_efforts"])
            self.assertNotIn("xhigh", responses["supported_efforts"])

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
