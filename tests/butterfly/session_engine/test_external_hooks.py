"""Tests for butterfly.session_engine.external_hooks."""
from __future__ import annotations

import json
import pytest

from butterfly.session_engine.external_hooks import (
    VALID_EVENTS,
    main_script_path,
    run_hooks,
)


def _prep(hook_dir, event: str, body: str) -> None:
    (hook_dir / event).mkdir(parents=True, exist_ok=True)
    main_script_path(hook_dir, event).write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")


async def _collect(events_list):
    def emit(ev: dict) -> None:
        events_list.append(ev)
    return emit


@pytest.mark.asyncio
async def test_missing_main_sh_is_noop(tmp_path):
    events = []
    await run_hooks(
        "session_start",
        {},
        hook_dir=tmp_path / "hook",
        session_id="sess",
        cwd=tmp_path,
        emit_event=events.append,
    )
    assert events == []


@pytest.mark.asyncio
async def test_main_sh_receives_stdin_json(tmp_path):
    hook_dir = tmp_path / "hook"
    _prep(hook_dir, "agent_loop_start", "cat > " + str(tmp_path / "got.json"))
    events: list[dict] = []
    await run_hooks(
        "agent_loop_start",
        {"content": "hello", "source": "user"},
        hook_dir=hook_dir,
        session_id="sess-1",
        cwd=tmp_path,
        emit_event=events.append,
    )
    assert events and events[0]["type"] == "hook_run"
    assert events[0]["exit_code"] == 0
    payload = json.loads((tmp_path / "got.json").read_text())
    assert payload["event"] == "agent_loop_start"
    assert payload["session_id"] == "sess-1"
    assert payload["data"]["content"] == "hello"


@pytest.mark.asyncio
async def test_invalid_event_skipped(tmp_path):
    hook_dir = tmp_path / "hook"
    _prep(hook_dir, "bogus", "echo hi")
    events: list[dict] = []
    await run_hooks(
        "bogus",  # not in VALID_EVENTS
        {},
        hook_dir=hook_dir,
        session_id="sess",
        cwd=tmp_path,
        emit_event=events.append,
    )
    assert events == []


@pytest.mark.asyncio
async def test_non_zero_exit_is_observed_not_raised(tmp_path):
    hook_dir = tmp_path / "hook"
    _prep(hook_dir, "agent_loop_end", "exit 3")
    events: list[dict] = []
    await run_hooks(
        "agent_loop_end",
        {"reason": "finished"},
        hook_dir=hook_dir,
        session_id="sess",
        cwd=tmp_path,
        emit_event=events.append,
    )
    assert events[0]["exit_code"] == 3


@pytest.mark.asyncio
async def test_timeout_killed_and_logged(tmp_path, monkeypatch):
    # Keep test fast: drop the timeout for this run.
    import butterfly.session_engine.external_hooks as eh
    monkeypatch.setattr(eh, "_HOOK_TIMEOUT_SEC", 0.5)
    hook_dir = tmp_path / "hook"
    _prep(hook_dir, "agent_loop_start", "sleep 5; echo done")
    events: list[dict] = []
    await run_hooks(
        "agent_loop_start",
        {},
        hook_dir=hook_dir,
        session_id="sess",
        cwd=tmp_path,
        emit_event=events.append,
    )
    assert events[0].get("timed_out") is True


@pytest.mark.asyncio
async def test_cwd_is_session_root(tmp_path):
    hook_dir = tmp_path / "hook"
    _prep(hook_dir, "session_start", "pwd > pwd.txt")
    events: list[dict] = []
    await run_hooks(
        "session_start",
        {"resumed": False},
        hook_dir=hook_dir,
        session_id="sess",
        cwd=tmp_path,
        emit_event=events.append,
    )
    assert (tmp_path / "pwd.txt").read_text().strip() == str(tmp_path)


def test_valid_events_exposes_only_three():
    assert VALID_EVENTS == frozenset({"session_start", "agent_loop_start", "agent_loop_end"})
