"""Tests for send_to_session built-in tool."""
from __future__ import annotations

import importlib
import json
import sys
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_session_msg():
    import nutshell.tool_engine.providers.session_msg as mod
    return importlib.reload(mod)


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


@pytest.mark.asyncio
async def test_async_message_sent(tmp_path, monkeypatch):
    mod = _load_session_msg()
    session_id = "peer-001"
    target = tmp_path / session_id
    target.mkdir(parents=True)
    (target / "manifest.json").write_text("{}", encoding="utf-8")

    calls = []

    class DummyResponse:
        status_code = 200
        text = "ok"

        def json(self):
            return {"ok": True}

    class DummyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            calls.append((url, json))
            return DummyResponse()

    monkeypatch.setattr(httpx, "AsyncClient", DummyClient)

    result = await mod.send_to_session(
        session_id=session_id,
        message="Hello there",
        mode="async",
        _system_base=tmp_path,
        _qjbq_base_url="http://qjbq.test",
    )

    assert result == f"Message sent to session {session_id}."
    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "http://qjbq.test/api/session-message"
    assert payload["session_id"] == session_id
    assert payload["content"] == "Hello there"
    assert payload["caller"] == "agent"
    assert payload["mode"] == "async"
    assert payload["message_id"]
    assert payload["ts"]


@pytest.mark.asyncio
async def test_sync_message_waits_for_reply(tmp_path, monkeypatch):
    mod = _load_session_msg()
    session_id = "peer-002"
    target = tmp_path / session_id
    target.mkdir(parents=True)
    (target / "manifest.json").write_text("{}", encoding="utf-8")
    ctx = target / "context.jsonl"

    fixed_id = "msg-123"
    monkeypatch.setattr(mod.uuid, "uuid4", lambda: fixed_id)

    class DummyResponse:
        status_code = 200
        text = "ok"

        def json(self):
            return {"ok": True}

    class DummyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            _write_jsonl(ctx, [{
                "type": "turn",
                "user_input_id": fixed_id,
                "messages": [{"role": "assistant", "content": "Roger that"}],
            }])
            return DummyResponse()

    monkeypatch.setattr(httpx, "AsyncClient", DummyClient)

    result = await mod.send_to_session(
        session_id=session_id,
        message="Status?",
        mode="sync",
        timeout=1,
        _system_base=tmp_path,
        _qjbq_base_url="http://qjbq.test",
    )

    assert result == "Roger that"


@pytest.mark.asyncio
async def test_send_to_missing_session(tmp_path):
    mod = _load_session_msg()
    result = await mod.send_to_session(
        session_id="nope",
        message="Ping",
        _system_base=tmp_path,
    )
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_send_to_self_blocked(tmp_path, monkeypatch):
    mod = _load_session_msg()
    session_id = "self-001"
    monkeypatch.setenv("NUTSHELL_SESSION_ID", session_id)
    result = await mod.send_to_session(
        session_id=session_id,
        message="Hello me",
        _system_base=tmp_path,
    )
    assert "cannot send to own session" in result.lower()


@pytest.mark.asyncio
async def test_sync_timeout(tmp_path, monkeypatch):
    mod = _load_session_msg()
    session_id = "peer-003"
    target = tmp_path / session_id
    target.mkdir(parents=True)
    (target / "manifest.json").write_text("{}", encoding="utf-8")

    class DummyResponse:
        status_code = 200
        text = "ok"

        def json(self):
            return {"ok": True}

    class DummyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            return DummyResponse()

    monkeypatch.setattr(httpx, "AsyncClient", DummyClient)

    result = await mod.send_to_session(
        session_id=session_id,
        message="Anyone there?",
        mode="sync",
        timeout=0.1,
        _system_base=tmp_path,
        _qjbq_base_url="http://qjbq.test",
    )
    assert "timeout" in result.lower()


def test_find_turn_returns_reply(tmp_path):
    mod = _load_session_msg()
    ctx = tmp_path / "context.jsonl"
    msg_id = str(uuid.uuid4())
    _write_jsonl(ctx, [
        {"type": "user_input", "id": msg_id, "content": "Hi"},
        {
            "type": "turn",
            "user_input_id": msg_id,
            "messages": [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello!"},
            ],
        },
    ])
    assert mod._find_turn(ctx, msg_id) == "Hello!"


def test_find_turn_empty_if_no_assistant_text(tmp_path):
    mod = _load_session_msg()
    ctx = tmp_path / "context.jsonl"
    msg_id = str(uuid.uuid4())
    _write_jsonl(ctx, [{"type": "turn", "user_input_id": msg_id, "messages": []}])
    assert mod._find_turn(ctx, msg_id) == ""


def test_find_turn_ignores_bad_json(tmp_path):
    mod = _load_session_msg()
    ctx = tmp_path / "context.jsonl"
    msg_id = str(uuid.uuid4())
    ctx.write_text(
        "not-json\n"
        + json.dumps({
            "type": "turn",
            "user_input_id": msg_id,
            "messages": [{"role": "assistant", "content": "OK"}],
        })
        + "\n",
        encoding="utf-8",
    )
    assert mod._find_turn(ctx, msg_id) == "OK"


def test_send_to_session_registered_as_builtin():
    from nutshell.tool_engine.registry import get_builtin

    impl = get_builtin("send_to_session")
    assert impl is not None
    assert callable(impl)
