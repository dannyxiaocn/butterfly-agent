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
        """Phase 7: /history returns events_v1 display events (for_llm +
        UI-visible system types). Plumbing events (``control_*``,
        ``session_*``) must be filtered out."""
        from butterfly.runtime.events import (
            EVENT_AGENT_TEXT,
            EVENT_CONTROL_START,
            EVENT_USER_INPUT,
            append_event,
        )
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            system_dir = root / "_sessions" / "test-session"
            append_event(system_dir, EVENT_USER_INPUT, {"text": "hello"})
            append_event(system_dir, EVENT_AGENT_TEXT, {"text": "hi there", "model": "m"})
            append_event(system_dir, EVENT_CONTROL_START, {})  # plumbing
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.get("/api/sessions/test-session/history")

            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            types = [ev["type"] for ev in payload["events"]]
            self.assertEqual(types, ["user_input", "agent_text"])
            self.assertEqual(payload["events"][0]["payload"]["text"], "hello")
            self.assertEqual(payload["events"][1]["payload"]["text"], "hi there")

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
            save_card(tasks_dir, TaskCard(name="duty", description="check inbox", check_interval=300))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.get("/api/sessions/test-session/tasks")
            self.assertEqual(resp.status_code, 200)
            cards = resp.json()["cards"]
            duty = next(c for c in cards if c["name"] == "duty")
            self.assertEqual(duty["description"], "check inbox")
            self.assertEqual(duty["check_interval"], 300)

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

    def test_stop_and_start_emit_task_card_changed_events(self) -> None:
        """v2.0.30 — POST /stop pauses every active card AND emits one
        ``task_card_changed`` per flipped card; POST /start flips them
        back to pending + emits ``resumed`` events. Drives the web UI's
        on-event Tasks tab refresh when the user clicks Stop/Start."""
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            tasks_dir = root / "sessions" / "test-session" / "core" / "tasks"
            save_card(tasks_dir, TaskCard(name="a", description="", status="pending"))
            save_card(tasks_dir, TaskCard(name="b", description="", status="working"))
            save_card(tasks_dir, TaskCard(name="c", description="", status="finished"))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                self.assertEqual(client.post("/api/sessions/test-session/stop").status_code, 200)
            events_path = root / "_sessions" / "test-session" / "events.jsonl"
            lines = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
            paused = [e for e in lines if e.get("type") == "task_card_changed" and e.get("change") == "paused"]
            self.assertEqual(
                sorted(e["card"] for e in paused),
                ["a", "b"],
                "stop emits one paused event per flipped card; finished cards untouched",
            )

            with TestClient(app) as client:
                self.assertEqual(client.post("/api/sessions/test-session/start").status_code, 200)
            lines = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
            resumed = [e for e in lines if e.get("type") == "task_card_changed" and e.get("change") == "resumed"]
            self.assertEqual(
                sorted(e["card"] for e in resumed),
                ["a", "b"],
                "start emits one resumed event per flipped card",
            )

    def test_put_tasks_emits_task_card_changed_event(self) -> None:
        """Phase 7: web-side PUT /tasks writes one ``task_card_changed``
        event to events_v1.jsonl per mutation. The ``card`` payload is
        None for deletes; otherwise the full projected card. Drives the
        frontend's on-event Tasks-tab refresh."""
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.put(
                    "/api/sessions/test-session/tasks",
                    json={"name": "duty", "description": "check"},
                )
                self.assertEqual(resp.status_code, 200)
                resp = client.put(
                    "/api/sessions/test-session/tasks",
                    json={"name": "duty", "description": "check v2"},
                )
                self.assertEqual(resp.status_code, 200)
                resp = client.delete("/api/sessions/test-session/tasks/duty")
                self.assertEqual(resp.status_code, 200)
            events_path = root / "_sessions" / "test-session" / "events_v1.jsonl"
            lines = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
            changed = [e for e in lines if e.get("type") == "task_card_changed"]
            self.assertEqual(len(changed), 3)
            self.assertEqual(changed[0]["payload"]["name"], "duty")
            self.assertIsNotNone(changed[0]["payload"]["card"])  # create
            self.assertEqual(changed[1]["payload"]["name"], "duty")
            self.assertEqual(changed[1]["payload"]["card"]["description"], "check v2")
            self.assertEqual(changed[2]["payload"]["name"], "duty")
            self.assertIsNone(changed[2]["payload"]["card"])  # delete sentinel

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
                    check_interval=600,
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
            self.assertEqual(card["check_interval"], 600)
            self.assertEqual(card["status"], "paused")
            self.assertEqual(card["last_finished_at"], "2026-04-09T10:00:00")
            self.assertEqual(card["created_at"], "2026-04-08T09:00:00")

    def test_put_tasks_by_name_updates_check_interval(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.put(
                    "/api/sessions/test-session/tasks",
                    json={
                        "name": "duty",
                        "description": "check messages",
                        "check_interval": 900,
                    },
                )
                self.assertEqual(resp.status_code, 200)
                cards = client.get("/api/sessions/test-session/tasks").json()["cards"]
            duty = next(c for c in cards if c["name"] == "duty")
            self.assertEqual(duty["check_interval"], 900)

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

    def test_api_update_status_endpoint_removed(self) -> None:
        """PR #52 deleted the auto-update worker + its ``/api/update_status``
        endpoint. The endpoint must return 404 (not 200 with ``{}``), so a
        stale frontend polling against an old build never triggers a reload
        loop — and so nothing on the server side quietly keeps servicing a
        feature that was supposed to be retired.
        """
        with TemporaryDirectory() as td:
            root = Path(td)
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                response = client.get("/api/update_status")
            self.assertEqual(response.status_code, 404)


class Phase7Tests(unittest.TestCase):
    """Phase 7 invariants — web is a thin shell over butterfly.runtime.io."""

    # ── app.py import hygiene (no direct file IO, no service imports) ────

    def test_no_direct_file_io_from_app_py(self) -> None:
        """app.py may only import from ``butterfly.runtime`` + stdlib +
        FastAPI. No ``butterfly.service.*`` imports and no raw file IO
        helpers (``open(``, ``Path(...`` in disk-touching paths, ``json.load``).
        The one allowed ``Path(...)`` usage is module-level path
        constants; we check the IMPORT surface instead of substring
        matching.
        """
        import ast
        src_path = Path(__file__).resolve().parent.parent.parent.parent / "ui" / "web" / "app.py"
        source = src_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        banned_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("butterfly.service"):
                    banned_modules.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("butterfly.service"):
                        banned_modules.add(alias.name)
        self.assertEqual(
            banned_modules,
            set(),
            f"app.py imports banned service modules: {banned_modules}",
        )
        # Soft grep: we do use Path for the dist / sessions-dir constants,
        # but no runtime-path handler should touch open() or json.load().
        self.assertNotIn("json.load(", source)
        self.assertNotIn("json.loads(f.read", source)

    # ── SSE stream ───────────────────────────────────────────────────────

    async def _collect_sse_frames_via_asgi(
        self,
        app,
        path: str,
        *,
        expected_body_chunks: int = 4,
        timeout: float = 5.0,
        headers: dict | None = None,
    ) -> tuple[int, list[str]]:
        """Drive the ASGI app directly until ``expected_body_chunks``
        non-comment SSE frames arrive, then signal http.disconnect.

        Bypasses httpx's ASGI transport (which buffers SSE bodies past
        the first yield). We send scope+receive/send messages as raw
        dicts — the same protocol uvicorn uses — so the StreamingResponse
        generator runs its full yield cycle in the test event loop.
        """
        import asyncio
        assert path.startswith("/")
        query = b""
        if "?" in path:
            p, _, q = path.partition("?")
            path = p
            query = q.encode()
        received_events: list[dict] = []
        send_done = asyncio.Event()
        disconnect_sent = asyncio.Event()

        async def receive():
            # One initial 'http.request', then 'http.disconnect' once
            # we've collected enough frames.
            if not disconnect_sent.is_set():
                await asyncio.sleep(0)  # let the server run
                if not disconnect_sent.is_set():
                    return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(msg):
            received_events.append(msg)
            if msg.get("type") == "http.response.start":
                pass
            elif msg.get("type") == "http.response.body":
                # Count non-comment frames.
                body = msg.get("body", b"")
                frames = sum(
                    1 for frame in body.decode("utf-8", errors="replace").split("\n\n")
                    if frame.strip() and not frame.startswith(":")
                )
                nonlocal_body_frames[0] += frames
                if nonlocal_body_frames[0] >= expected_body_chunks:
                    disconnect_sent.set()
                if not msg.get("more_body", True):
                    send_done.set()

        nonlocal_body_frames = [0]
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query,
            "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "client": ("127.0.0.1", 0),
            "server": ("127.0.0.1", 80),
        }
        try:
            await asyncio.wait_for(app(scope, receive, send), timeout=timeout)
        except asyncio.TimeoutError:
            pass

        # Extract status + decoded frames from the send log.
        status = 0
        body_chunks: list[str] = []
        for ev in received_events:
            if ev["type"] == "http.response.start":
                status = ev["status"]
            elif ev["type"] == "http.response.body":
                body_chunks.append(ev["body"].decode("utf-8", errors="replace"))
        joined = "".join(body_chunks)
        frames = [
            frame for frame in joined.split("\n\n")
            if frame.strip() and not frame.startswith(":")
        ]
        return status, frames

    def test_sse_stream_yields_events_in_order(self) -> None:
        """One Event = one SSE frame. ``id:`` = Event id, ``event:`` =
        Event type, ``data:`` = full Event JSON."""
        import asyncio
        from butterfly.runtime.events import (
            EVENT_AGENT_TEXT,
            EVENT_USER_INPUT,
            append_event,
        )
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            system_dir = root / "_sessions" / "test-session"
            append_event(system_dir, EVENT_USER_INPUT, {"text": "hi"})
            append_event(system_dir, EVENT_AGENT_TEXT, {"text": "hello", "model": "m"})
            append_event(system_dir, EVENT_USER_INPUT, {"text": "again"})
            app = create_app(root / "sessions", root / "_sessions")
            with patch("ui.web.app._SSE_KEEPALIVE_SECONDS", 0.3):
                status, frames = asyncio.run(
                    self._collect_sse_frames_via_asgi(
                        app,
                        "/api/sessions/test-session/events/stream",
                        expected_body_chunks=3,
                    )
                )
            self.assertEqual(status, 200)
            self.assertGreaterEqual(len(frames), 3)
            ids, types = [], []
            for frame in frames[:3]:
                lines = dict(
                    line.split(": ", 1) for line in frame.splitlines()
                    if ": " in line
                )
                ids.append(int(lines["id"]))
                types.append(lines["event"])
                data = json.loads(lines["data"])
                self.assertIn("id", data)
                self.assertIn("type", data)
                self.assertIn("payload", data)
            self.assertEqual(ids, [1, 2, 3])
            self.assertEqual(types, ["user_input", "agent_text", "user_input"])

    def test_sse_resume_from_cursor_query_param(self) -> None:
        """``?cursor=N`` yields only events with id > N."""
        import asyncio
        from butterfly.runtime.events import EVENT_USER_INPUT, append_event
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            system_dir = root / "_sessions" / "test-session"
            for i in range(5):
                append_event(system_dir, EVENT_USER_INPUT, {"text": f"m{i}"})
            app = create_app(root / "sessions", root / "_sessions")
            with patch("ui.web.app._SSE_KEEPALIVE_SECONDS", 0.3):
                status, frames = asyncio.run(
                    self._collect_sse_frames_via_asgi(
                        app,
                        "/api/sessions/test-session/events/stream?cursor=3",
                        expected_body_chunks=2,
                    )
                )
            self.assertEqual(status, 200)
            ids = []
            for frame in frames[:2]:
                lines = dict(
                    line.split(": ", 1) for line in frame.splitlines()
                    if ": " in line
                )
                ids.append(int(lines["id"]))
            self.assertEqual(ids, [4, 5])

    def test_sse_resume_from_last_event_id_header(self) -> None:
        """``Last-Event-ID: N`` header is the SSE reconnect contract;
        the server resumes from id > N even when the query param is
        absent."""
        import asyncio
        from butterfly.runtime.events import EVENT_USER_INPUT, append_event
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            system_dir = root / "_sessions" / "test-session"
            for i in range(4):
                append_event(system_dir, EVENT_USER_INPUT, {"text": f"m{i}"})
            app = create_app(root / "sessions", root / "_sessions")
            with patch("ui.web.app._SSE_KEEPALIVE_SECONDS", 0.3):
                status, frames = asyncio.run(
                    self._collect_sse_frames_via_asgi(
                        app,
                        "/api/sessions/test-session/events/stream",
                        expected_body_chunks=2,
                        headers={"last-event-id": "2"},
                    )
                )
            self.assertEqual(status, 200)
            ids = [
                int(
                    dict(
                        ln.split(": ", 1) for ln in f.splitlines() if ": " in ln
                    )["id"]
                )
                for f in frames[:2]
            ]
            self.assertEqual(ids, [3, 4])

    def test_sse_stream_404_on_missing_session(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "sessions").mkdir()
            (root / "_sessions").mkdir()
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.get("/api/sessions/missing/events/stream")
            self.assertEqual(resp.status_code, 404)

    # ── /events (JSON replay) ────────────────────────────────────────────

    def test_events_endpoint_returns_all_events_as_json(self) -> None:
        from butterfly.runtime.events import (
            EVENT_CONTROL_START,
            EVENT_USER_INPUT,
            append_event,
        )
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            system_dir = root / "_sessions" / "test-session"
            append_event(system_dir, EVENT_USER_INPUT, {"text": "hi"})
            append_event(system_dir, EVENT_CONTROL_START, {})
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.get("/api/sessions/test-session/events")
            self.assertEqual(resp.status_code, 200)
            events = resp.json()["events"]
            # /events returns EVERY event (no filter) including control_* plumbing.
            self.assertEqual([e["type"] for e in events], ["user_input", "control_start"])

    def test_events_since_id_filter(self) -> None:
        from butterfly.runtime.events import EVENT_USER_INPUT, append_event
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            system_dir = root / "_sessions" / "test-session"
            for i in range(3):
                append_event(system_dir, EVENT_USER_INPUT, {"text": f"m{i}"})
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.get("/api/sessions/test-session/events?since_id=1")
            events = resp.json()["events"]
            self.assertEqual([e["id"] for e in events], [2, 3])

    # ── /interrupt ───────────────────────────────────────────────────────

    def test_interrupt_endpoint_appends_event(self) -> None:
        from butterfly.runtime.events import EVENT_USER_INTERRUPT, read_events
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.post("/api/sessions/test-session/interrupt")
            self.assertEqual(resp.status_code, 200)
            system_dir = root / "_sessions" / "test-session"
            types = [e.type for e in read_events(system_dir)]
            self.assertIn(EVENT_USER_INTERRUPT, types)

    # ── /messages ────────────────────────────────────────────────────────

    def test_messages_endpoint_appends_user_input_event(self) -> None:
        from butterfly.runtime.events import EVENT_USER_INPUT, read_events
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.post(
                    "/api/sessions/test-session/messages",
                    json={"content": "hello world"},
                )
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertIn("event_id", body)
            system_dir = root / "_sessions" / "test-session"
            user_inputs = [e for e in read_events(system_dir) if e.type == EVENT_USER_INPUT]
            # io.send_message appends once; the messages_service delegation
            # may append a second. Either way, at least one must carry the
            # web-sent text.
            self.assertTrue(
                any(e.payload.get("text") == "hello world" for e in user_inputs),
                f"expected user_input with text 'hello world', got {user_inputs}",
            )

    def test_messages_endpoint_rejects_meta_session(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td), session_id="agent_meta")
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.post(
                    "/api/sessions/agent_meta/messages",
                    json={"content": "hi"},
                )
            self.assertEqual(resp.status_code, 403)

    # ── /todo_list ──────────────────────────────────────────────────────

    def test_todo_list_round_trip(self) -> None:
        with TemporaryDirectory() as td:
            root = _make_session(Path(td))
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                post = client.post(
                    "/api/sessions/test-session/todo_list",
                    json={"todo_list": {
                        "todos": [
                            {"content": "one", "status": "pending",
                             "activeForm": "doing one"},
                        ],
                    }},
                )
                self.assertEqual(post.status_code, 200)
                got = client.get("/api/sessions/test-session/todo_list")
            self.assertEqual(got.status_code, 200)
            payload = got.json()["todo_list"]
            self.assertIsNotNone(payload)
            self.assertEqual(payload["total"], 1)

    # ── Catalogs ─────────────────────────────────────────────────────────

    def test_models_endpoint_returns_providers_wrapper(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "sessions").mkdir()
            (root / "_sessions").mkdir()
            app = create_app(root / "sessions", root / "_sessions")
            with TestClient(app) as client:
                resp = client.get("/api/models")
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            self.assertIn("providers", payload)
            self.assertIsInstance(payload["providers"], list)
