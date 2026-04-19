"""End-to-end coverage for the v2.0.27 bash-driven task card flow.

Exercises Session._poll_trigger_script / _poll_end_script in isolation
(no LLM) so we cover: [start] → seed propagation, [skip] → no fire,
non-zero exit → fail-closed, [done] → mark_terminal.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from butterfly.core.agent import Agent
from butterfly.session_engine.session import Session
from butterfly.session_engine.task_cards import (
    TaskCard,
    load_card,
    save_card,
    write_end_script,
    write_trigger_script,
)


class _NoProvider:
    """Agents in these tests never call .run(); provider is unused."""

    async def complete(self, *args, **kwargs):  # pragma: no cover - never called
        raise AssertionError("provider.complete() should not fire")


def _make_session(tmp_path: Path) -> Session:
    agent = Agent(provider=_NoProvider())
    return Session(
        agent,
        session_id="bash-tasks",
        base_dir=tmp_path / "sessions",
        system_base=tmp_path / "_sessions",
    )


@pytest.mark.asyncio
async def test_poll_trigger_start_returns_seed(tmp_path):
    session = _make_session(tmp_path)
    card = TaskCard(name="build", description="watch build", check_interval=30)
    save_card(session.tasks_dir, card)
    write_trigger_script(session.tasks_dir, "build", 'echo "[start] build ready"')
    seed = await session._poll_trigger_script(card)
    assert seed == "build ready"
    # last_checked_at should have been stamped + persisted
    refreshed = load_card(session.tasks_dir, "build")
    assert refreshed is not None
    assert refreshed.last_checked_at is not None


@pytest.mark.asyncio
async def test_poll_trigger_skip_returns_none(tmp_path):
    session = _make_session(tmp_path)
    card = TaskCard(name="idle", check_interval=30)
    save_card(session.tasks_dir, card)
    write_trigger_script(session.tasks_dir, "idle", "echo [skip]")
    seed = await session._poll_trigger_script(card)
    assert seed is None


@pytest.mark.asyncio
async def test_poll_trigger_fail_closed_on_non_zero(tmp_path):
    session = _make_session(tmp_path)
    card = TaskCard(name="broken", check_interval=30)
    save_card(session.tasks_dir, card)
    write_trigger_script(session.tasks_dir, "broken", "echo [start]; exit 1")
    seed = await session._poll_trigger_script(card)
    assert seed is None
    # A task_check_error must have been emitted.
    events = [
        line for line in session._events_path.read_text(encoding="utf-8").splitlines()
        if "task_check_error" in line
    ]
    assert events, "non-zero trigger exit must emit task_check_error"


@pytest.mark.asyncio
async def test_poll_end_done_flag(tmp_path):
    session = _make_session(tmp_path)
    card = TaskCard(name="watch", status="working", check_interval=30)
    save_card(session.tasks_dir, card)
    write_end_script(session.tasks_dir, "watch", "echo [done]")
    assert await session._poll_end_script(card) is True


@pytest.mark.asyncio
async def test_poll_end_missing_script_is_noop(tmp_path):
    session = _make_session(tmp_path)
    card = TaskCard(name="endless", status="working", check_interval=30)
    save_card(session.tasks_dir, card)
    # no end.sh on disk
    assert await session._poll_end_script(card) is False


@pytest.mark.asyncio
async def test_poll_end_not_done(tmp_path):
    session = _make_session(tmp_path)
    card = TaskCard(name="patient", status="working", check_interval=30)
    save_card(session.tasks_dir, card)
    write_end_script(session.tasks_dir, "patient", "echo [not_done]")
    assert await session._poll_end_script(card) is False


@pytest.mark.asyncio
async def test_task_check_event_captures_output(tmp_path):
    session = _make_session(tmp_path)
    card = TaskCard(name="chatty", check_interval=30)
    save_card(session.tasks_dir, card)
    write_trigger_script(session.tasks_dir, "chatty", "echo debug info; echo [start]")
    await session._poll_trigger_script(card)
    events = [
        line for line in session._events_path.read_text(encoding="utf-8").splitlines()
        if '"type": "task_check"' in line
    ]
    assert events, "task_check event must be written"
    assert "debug info" in events[-1]
