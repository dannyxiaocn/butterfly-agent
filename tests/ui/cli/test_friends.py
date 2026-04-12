"""Tests for ui.cli.friends — IM-style session status list."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import pytest

from ui.cli.friends import (
    classify_status,
    build_friends_list,
    format_friends_table,
    format_friends_json,
)


def _ts(seconds_ago: float) -> str:
    """Return an ISO timestamp `seconds_ago` seconds in the past."""
    dt = datetime.now(tz=timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.isoformat()


# ── classify_status ───────────────────────────────────────────────────────────

class TestClassifyStatus:
    """Tests for classify_status() — the core status engine."""

    def test_online_when_model_running(self):
        """model_state='running' → online regardless of last_run_at."""
        info = {"model_state": "running", "status": "active"}
        assert classify_status(info) == "online"

    def test_online_when_recent_last_run(self):
        """last_run_at within 5 minutes → online."""
        info = {"last_run_at": _ts(60), "status": "active"}
        assert classify_status(info) == "online"

    def test_idle_when_last_run_within_hour(self):
        """last_run_at between 5 min and 1 hour → idle."""
        info = {"last_run_at": _ts(1800), "status": "active"}  # 30 min ago
        assert classify_status(info) == "idle"

    def test_offline_when_last_run_old(self):
        """last_run_at > 1 hour → offline."""
        info = {"last_run_at": _ts(7200), "status": "active"}  # 2 hours ago
        assert classify_status(info) == "offline"

    def test_offline_when_stopped(self):
        """status='stopped' → offline, even if last_run_at is recent."""
        info = {"last_run_at": _ts(10), "status": "stopped", "model_state": "running"}
        assert classify_status(info) == "offline"

    def test_offline_when_no_last_run(self):
        """No last_run_at and not running → offline."""
        info = {"status": "active"}
        assert classify_status(info) == "offline"


# ── format_friends_table ──────────────────────────────────────────────────────

class TestFormatFriendsTable:
    """Tests for the human-readable table output."""

    def test_output_contains_status_dots(self):
        """Each friend line should contain the correct status dot."""
        sessions = [
            {"id": "s1", "entity": "agent", "model_state": "running", "status": "active",
             "last_run_at": _ts(10)},
            {"id": "s2", "entity": "dev", "status": "active",
             "last_run_at": _ts(1800)},
            {"id": "s3", "entity": "agent", "status": "active",
             "last_run_at": _ts(7200)},
        ]
        friends = build_friends_list(sessions)
        table = format_friends_table(friends)

        lines = table.strip().split("\n")
        assert len(lines) == 3

        # Online first, then idle, then offline
        assert "●" in lines[0] and "online" in lines[0]
        assert "◐" in lines[1] and "idle" in lines[1]
        assert "○" in lines[2] and "offline" in lines[2]

    def test_empty_sessions(self):
        """No sessions → friendly message."""
        assert format_friends_table([]) == "No sessions found."
        friends = build_friends_list([])
        assert format_friends_table(friends) == "No sessions found."


# ── format_friends_json ───────────────────────────────────────────────────────

class TestFormatFriendsJson:
    """Tests for the JSON output format."""

    def test_json_output_is_valid_and_has_required_fields(self):
        """JSON output parses correctly and includes status, id, entity."""
        sessions = [
            {"id": "2026-03-25_10-00-00", "entity": "agent",
             "model_state": "running", "status": "active",
             "last_run_at": _ts(30)},
        ]
        friends = build_friends_list(sessions)
        raw = format_friends_json(friends)
        data = json.loads(raw)

        assert isinstance(data, list)
        assert len(data) == 1
        entry = data[0]
        assert entry["id"] == "2026-03-25_10-00-00"
        assert entry["entity"] == "agent"
        assert entry["status"] == "online"
        assert "last_ago" in entry
        assert "last_run_at" in entry
        assert "model_state" in entry
