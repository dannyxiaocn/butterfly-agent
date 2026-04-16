from __future__ import annotations

import json

import pytest

from butterfly.runtime.ipc import FileIPC
from butterfly.service.history_service import (
    get_history,
    get_log_turns,
    get_pending_inputs,
    get_token_report,
)
from butterfly.session_engine.session_status import write_session_status


def _seed_context(tmp_path, session_id: str, events: list[dict]) -> tuple:
    sessions_dir = tmp_path / "sessions"
    system_dir = tmp_path / "_sessions" / session_id
    (sessions_dir / session_id / "core").mkdir(parents=True)
    system_dir.mkdir(parents=True)
    (system_dir / "manifest.json").write_text(json.dumps({"entity": "agent"}), encoding="utf-8")
    (system_dir / "context.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    return sessions_dir, tmp_path / "_sessions"


def test_get_pending_inputs_returns_only_unpaired_inputs_in_order(tmp_path):
    _, system_sessions_dir = _seed_context(
        tmp_path,
        "pending-session",
        [
            {"type": "user_input", "id": "u1", "content": "first", "ts": "2026-03-25T10:00:00"},
            {"type": "turn", "user_input_id": "u1", "ts": "2026-03-25T10:00:05", "messages": []},
            {"type": "user_input", "id": "u2", "content": "second", "ts": "2026-03-25T10:01:00"},
            {"type": "user_input", "id": "u3", "content": "third", "ts": "2026-03-25T10:02:00"},
        ],
    )

    rows = get_pending_inputs("pending-session", system_sessions_dir)

    assert rows == [
        {"ts": "2026-03-25 10:01", "user": "second"},
        {"ts": "2026-03-25 10:02", "user": "third"},
    ]


def test_get_pending_inputs_respects_limit(tmp_path):
    _, system_sessions_dir = _seed_context(
        tmp_path,
        "pending-session",
        [
            {"type": "user_input", "id": "u1", "content": "first", "ts": "2026-03-25T10:00:00"},
            {"type": "user_input", "id": "u2", "content": "second", "ts": "2026-03-25T10:01:00"},
            {"type": "user_input", "id": "u3", "content": "third", "ts": "2026-03-25T10:02:00"},
        ],
    )

    rows = get_pending_inputs("pending-session", system_sessions_dir, n=2)

    assert rows == [
        {"ts": "2026-03-25 10:01", "user": "second"},
        {"ts": "2026-03-25 10:02", "user": "third"},
    ]


def test_get_pending_inputs_excludes_all_ids_consumed_by_merged_turn(tmp_path):
    _, system_sessions_dir = _seed_context(
        tmp_path,
        "pending-session",
        [
            {"type": "user_input", "id": "u1", "content": "first", "ts": "2026-03-25T10:00:00"},
            {"type": "user_input", "id": "u2", "content": "second", "ts": "2026-03-25T10:01:00"},
            {
                "type": "turn",
                "user_input_id": "u2",
                "merged_user_input_ids": ["u1", "u2"],
                "ts": "2026-03-25T10:01:05",
                "messages": [{"role": "assistant", "content": "merged"}],
            },
            {"type": "user_input", "id": "u3", "content": "third", "ts": "2026-03-25T10:02:00"},
        ],
    )

    rows = get_pending_inputs("pending-session", system_sessions_dir)

    assert rows == [{"ts": "2026-03-25 10:02", "user": "third"}]


def test_get_log_turns_and_token_report_use_merged_user_content(tmp_path):
    _, system_sessions_dir = _seed_context(
        tmp_path,
        "history-session",
        [
            {"type": "user_input", "id": "u1", "content": "first", "ts": "2026-03-25T10:00:00"},
            {"type": "user_input", "id": "u2", "content": "second", "ts": "2026-03-25T10:01:00"},
            {
                "type": "turn",
                "user_input_id": "u2",
                "merged_user_input_ids": ["u1", "u2"],
                "ts": "2026-03-25T10:01:05",
                "usage": {"input": 12, "output": 3},
                "messages": [{"role": "assistant", "content": "merged"}],
            },
        ],
    )

    rows = get_log_turns("history-session", system_sessions_dir)
    report = get_token_report("history-session", system_sessions_dir)

    assert rows[0]["ts"] == "2026-03-25 10:00"
    assert rows[0]["user"] == "first\n\nsecond"
    assert report[0]["ts"] == "2026-03-25 10:00"
    assert report[0]["trigger"] == "first\n\nsecond"


def test_get_pending_inputs_raises_for_missing_session(tmp_path):
    with pytest.raises(FileNotFoundError):
        get_pending_inputs("missing", tmp_path / "_sessions")


def test_get_history_returns_display_events_and_offsets(tmp_path):
    _, system_sessions_dir = _seed_context(
        tmp_path,
        "history-session",
        [
            {"type": "user_input", "id": "u1", "content": "hello", "ts": "2026-03-25T10:00:00"},
            {
                "type": "turn",
                "user_input_id": "u1",
                "ts": "2026-03-25T10:00:05",
                "messages": [{"role": "assistant", "content": "hi there"}],
            },
        ],
    )
    system_dir = system_sessions_dir / "history-session"
    ipc = FileIPC(system_dir)
    ipc.append_event({"type": "model_status", "state": "running", "ts": "2026-03-25T10:00:06"})
    write_session_status(system_dir, model_state="running")

    payload = get_history("history-session", system_sessions_dir)

    assert [event["type"] for event in payload["events"]] == ["user", "agent"]
    assert payload["events"][0]["content"] == "hello"
    assert payload["events"][1]["content"] == "hi there"
    assert payload["context_offset"] == system_dir.joinpath("context.jsonl").stat().st_size
    assert payload["events_offset"] == 0


def test_get_history_rejects_invalid_session_id(tmp_path):
    with pytest.raises(ValueError):
        get_history("bad.id", tmp_path / "_sessions")
