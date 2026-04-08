"""Tests for nutshell-chat CLI."""
from __future__ import annotations

import json
import sys
import threading
import time
import pytest
from pathlib import Path
from unittest.mock import patch


def _make_system_dir(tmp_path: Path, session_id: str) -> Path:
    sdir = tmp_path / session_id
    sdir.mkdir(parents=True)
    (sdir / "manifest.json").write_text(json.dumps({"entity": "agent"}))
    (sdir / "context.jsonl").write_text("")
    (sdir / "status.json").write_text(json.dumps({"status": "active"}))
    (sdir / "events.jsonl").write_text("")
    return sdir


# ── continue session ──────────────────────────────────────────────────────────

def test_continue_session_nonexistent_exits_1(tmp_path, capsys):
    from ui.cli.chat import main
    with pytest.raises(SystemExit) as exc:
        with patch("sys.argv", [
            "nutshell-chat", "--session", "ghost",
            "--system-base", str(tmp_path),
            "hi",
        ]):
            main()
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().err.lower()


def test_no_wait_writes_user_input_and_exits_0(tmp_path, capsys):
    sid = "my-session"
    sdir = _make_system_dir(tmp_path, sid)

    from ui.cli.chat import main
    with pytest.raises(SystemExit) as exc:
        with patch("sys.argv", [
            "nutshell-chat", "--session", sid,
            "--no-wait",
            "--system-base", str(tmp_path),
            "fire and forget",
        ]):
            main()
    assert exc.value.code == 0

    events = [
        json.loads(l) for l in (sdir / "context.jsonl").read_text().splitlines() if l.strip()
    ]
    assert any(
        e.get("type") == "user_input" and e.get("content") == "fire and forget"
        for e in events
    )
    assert capsys.readouterr().err == ""


def test_continue_session_timeout_exits_1(tmp_path, capsys):
    sid = "slow-session"
    _make_system_dir(tmp_path, sid)

    from ui.cli.chat import main
    with pytest.raises(SystemExit) as exc:
        with patch("sys.argv", [
            "nutshell-chat", "--session", sid,
            "--system-base", str(tmp_path),
            "--timeout", "0.3",
            "hello",
        ]):
            main()
    assert exc.value.code == 1
    assert "no response" in capsys.readouterr().err.lower()


def test_continue_session_prints_agent_reply(tmp_path, capsys):
    """A matching turn injected by a background thread is printed."""
    sid = "resp-session"
    sdir = _make_system_dir(tmp_path, sid)
    ctx_path = sdir / "context.jsonl"

    def _inject_response():
        for _ in range(40):
            time.sleep(0.05)
            for line in ctx_path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("type") == "user_input":
                    mid = ev.get("id", "")
                    turn = {
                        "type": "turn",
                        "triggered_by": "user",
                        "user_input_id": mid,
                        "messages": [{"role": "assistant", "content": "agent says hi"}],
                        "ts": "2026-01-01",
                    }
                    with ctx_path.open("a") as f:
                        f.write(json.dumps(turn) + "\n")
                    return

    t = threading.Thread(target=_inject_response, daemon=True)
    t.start()

    from ui.cli.chat import main
    with pytest.raises(SystemExit) as exc:
        with patch("sys.argv", [
            "nutshell-chat", "--session", sid,
            "--system-base", str(tmp_path),
            "--timeout", "5",
            "hello?",
        ]):
            main()

    t.join(timeout=3)
    assert exc.value.code == 0
    assert "agent says hi" in capsys.readouterr().out


# ── helper unit tests ─────────────────────────────────────────────────────────

def test_send_message_writes_user_input(tmp_path):
    from ui.cli.chat import _send_message
    ctx = tmp_path / "context.jsonl"
    ctx.write_text("")
    mid = _send_message(ctx, "test content")
    assert mid
    events = [json.loads(l) for l in ctx.read_text().splitlines() if l.strip()]
    assert len(events) == 1
    assert events[0]["type"] == "user_input"
    assert events[0]["content"] == "test content"
    assert events[0]["id"] == mid


def test_read_matching_turn_returns_none_for_wrong_id(tmp_path):
    from ui.cli.chat import _read_matching_turn
    ctx = tmp_path / "ctx.jsonl"
    ctx.write_text(json.dumps({
        "type": "turn",
        "user_input_id": "other",
        "messages": [{"role": "assistant", "content": "nope"}],
    }) + "\n")
    assert _read_matching_turn(ctx, "mine") is None


def test_read_matching_turn_returns_text(tmp_path):
    from ui.cli.chat import _read_matching_turn
    ctx = tmp_path / "ctx.jsonl"
    mid = "my-id"
    ctx.write_text(json.dumps({
        "type": "turn",
        "user_input_id": mid,
        "messages": [{"role": "assistant", "content": "hello world"}],
    }) + "\n")
    assert _read_matching_turn(ctx, mid) == "hello world"
