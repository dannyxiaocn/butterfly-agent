"""Tests for qjbq.server — FastAPI notification relay."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path):
    os.environ["QJBQ_SESSIONS_DIR"] = str(tmp_path / "sessions")
    os.environ["QJBQ_SYSTEM_SESSIONS_DIR"] = str(tmp_path / "_sessions")
    import importlib
    import cli_app.qjbq.server as server
    server = importlib.reload(server)
    with TestClient(server.app) as c:
        yield c
    os.environ.pop("QJBQ_SESSIONS_DIR", None)
    os.environ.pop("QJBQ_SYSTEM_SESSIONS_DIR", None)


@pytest.fixture()
def sessions_dir(tmp_path: Path) -> Path:
    return tmp_path / "sessions"


@pytest.fixture()
def system_sessions_dir(tmp_path: Path) -> Path:
    return tmp_path / "_sessions"


class TestHealth:
    def test_health_status(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"

    def test_health_version(self, client):
        r = client.get("/health")
        data = r.json()
        assert data["version"] == "0.1.0"


class TestPostNotify:
    def test_write_creates_file(self, client, sessions_dir):
        r = client.post("/api/notify", json={
            "session_id": "sess-001",
            "app": "alert",
            "content": "# Alert\nSomething happened.",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["chars"] == len("# Alert\nSomething happened.")
        f = sessions_dir / "sess-001" / "core" / "apps" / "alert.md"
        assert f.exists()
        assert f.read_text() == "# Alert\nSomething happened."

    def test_write_overwrites_existing(self, client, sessions_dir):
        client.post("/api/notify", json={
            "session_id": "sess-002",
            "app": "status",
            "content": "v1",
        })
        client.post("/api/notify", json={
            "session_id": "sess-002",
            "app": "status",
            "content": "v2 updated",
        })
        f = sessions_dir / "sess-002" / "core" / "apps" / "status.md"
        assert f.read_text() == "v2 updated"

    def test_write_empty_session_id_rejected(self, client):
        r = client.post("/api/notify", json={
            "session_id": "",
            "app": "test",
            "content": "hello",
        })
        assert r.status_code == 422

    def test_write_empty_content_rejected(self, client):
        r = client.post("/api/notify", json={
            "session_id": "sess-003",
            "app": "test",
            "content": "",
        })
        assert r.status_code == 422

    def test_write_invalid_app_name_rejected(self, client):
        r = client.post("/api/notify", json={
            "session_id": "sess-004",
            "app": "///...",
            "content": "hack",
        })
        assert r.status_code == 400
        assert "Invalid app name" in r.json()["detail"]

    def test_write_path_traversal_app_sanitized(self, client, sessions_dir):
        r = client.post("/api/notify", json={
            "session_id": "sess-005",
            "app": "../../etc",
            "content": "sanitized",
        })
        assert r.status_code == 200
        f = sessions_dir / "sess-005" / "core" / "apps" / "etc.md"
        assert f.exists()
        assert f.read_text() == "sanitized"

    def test_write_path_traversal_session_rejected(self, client):
        r = client.post("/api/notify", json={
            "session_id": "../../../etc",
            "app": "test",
            "content": "hack",
        })
        assert r.status_code == 400
        assert "Invalid session_id" in r.json()["detail"]


class TestGetNotifications:
    def test_empty_session_returns_empty_list(self, client):
        r = client.get("/api/notify/nonexistent-session")
        assert r.status_code == 200
        data = r.json()
        assert data["session_id"] == "nonexistent-session"
        assert data["notifications"] == []

    def test_lists_posted_notifications(self, client):
        client.post("/api/notify", json={
            "session_id": "sess-010",
            "app": "alpha",
            "content": "AAA",
        })
        client.post("/api/notify", json={
            "session_id": "sess-010",
            "app": "beta",
            "content": "BBB",
        })
        r = client.get("/api/notify/sess-010")
        assert r.status_code == 200
        data = r.json()
        assert len(data["notifications"]) == 2
        apps = [n["app"] for n in data["notifications"]]
        assert "alpha" in apps
        assert "beta" in apps

    def test_notification_content_matches(self, client):
        client.post("/api/notify", json={
            "session_id": "sess-011",
            "app": "memo",
            "content": "Remember this!",
        })
        r = client.get("/api/notify/sess-011")
        notifs = r.json()["notifications"]
        assert len(notifs) == 1
        assert notifs[0]["content"] == "Remember this!"
        assert notifs[0]["chars"] == len("Remember this!")

    def test_get_path_traversal_rejected(self, client):
        r = client.get("/api/notify/../../../etc")
        assert r.status_code in (400, 404, 422)


class TestSessionMessageRelay:
    def test_session_message_writes_context_event(self, client, system_sessions_dir):
        sess = system_sessions_dir / "peer-001"
        sess.mkdir(parents=True)
        (sess / "manifest.json").write_text("{}", encoding="utf-8")

        r = client.post("/api/session-message", json={
            "session_id": "peer-001",
            "content": "hello worker",
            "message_id": "msg-001",
            "caller": "agent",
            "mode": "sync",
            "ts": "2026-04-02T12:00:00",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["message_id"] == "msg-001"

        ctx = sess / "context.jsonl"
        lines = ctx.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event == {
            "type": "user_input",
            "content": "hello worker",
            "id": "msg-001",
            "caller": "agent",
            "mode": "sync",
            "ts": "2026-04-02T12:00:00",
        }

    def test_session_message_missing_session_rejected(self, client):
        r = client.post("/api/session-message", json={
            "session_id": "missing",
            "content": "hello",
            "message_id": "msg-404",
            "caller": "agent",
            "mode": "async",
        })
        assert r.status_code == 404
        assert "Session not found" in r.json()["detail"]

    def test_session_message_invalid_mode_rejected(self, client):
        r = client.post("/api/session-message", json={
            "session_id": "peer-002",
            "content": "hello",
            "message_id": "msg-bad",
            "caller": "agent",
            "mode": "later",
        })
        assert r.status_code == 422
