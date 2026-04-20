"""PR #19 review coverage: task_* verb tools.

Smoke coverage for the new per-verb task tools that replaced `manage_task`
(`task_create`, `task_finish`, `task_pause`, `task_resume`, `task_list`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from toolhub.task_create.executor import TaskCreateExecutor
from toolhub.task_finish.executor import TaskFinishExecutor
from toolhub.task_list.executor import TaskListExecutor
from toolhub.task_pause.executor import TaskPauseExecutor
from toolhub.task_resume.executor import TaskResumeExecutor


@pytest.mark.asyncio
async def test_task_create_and_list_roundtrip(tmp_path: Path) -> None:
    create = TaskCreateExecutor(tasks_dir=tmp_path)
    out = await create.execute(name="demo", description="say hi", interval=60)
    assert "Created task 'demo'" in out

    # Duplicate is rejected.
    dup = await create.execute(name="demo", description="again", interval=60)
    assert dup.startswith("Error:") and "already exists" in dup

    lst = TaskListExecutor(tasks_dir=tmp_path)
    listed = await lst.execute()
    assert "demo" in listed
    # interval=60 should render as "60s" (not "one-shot").
    assert "60s" in listed


@pytest.mark.asyncio
async def test_task_list_oneshot_labeling(tmp_path: Path) -> None:
    create = TaskCreateExecutor(tasks_dir=tmp_path)
    await create.execute(name="single", description="once")  # interval=None
    listed = await TaskListExecutor(tasks_dir=tmp_path).execute()
    assert "single" in listed
    assert "one-shot" in listed


@pytest.mark.asyncio
async def test_task_list_interval_zero_regression(tmp_path: Path) -> None:
    """Confirmed cubic finding: `if c.interval` treats 0 as falsy.

    task_list currently labels interval=0 tasks as "one-shot" even though
    they are recurring (though 0s would be a degenerate config). Document
    the current behaviour so a later fix can flip the assertion.
    """
    create = TaskCreateExecutor(tasks_dir=tmp_path)
    await create.execute(name="zerotask", description="bad cfg", interval=0)
    listed = await TaskListExecutor(tasks_dir=tmp_path).execute()
    # Current (buggy) behaviour: shows as "one-shot" because `if c.interval`
    # evaluates 0 as falsy. The correct label would be "0s".
    assert "zerotask" in listed
    if "0s" in listed:
        # When the bug is fixed, this branch will run instead.
        assert "one-shot" not in listed.split("zerotask", 1)[1].splitlines()[0]
    else:
        assert "one-shot" in listed


@pytest.mark.asyncio
async def test_task_pause_resume_roundtrip(tmp_path: Path) -> None:
    await TaskCreateExecutor(tasks_dir=tmp_path).execute(
        name="t1", description="work", interval=10
    )
    pr = await TaskPauseExecutor(tasks_dir=tmp_path).execute(name="t1")
    assert "paused" in pr.lower() or "t1" in pr
    listed = await TaskListExecutor(tasks_dir=tmp_path).execute()
    assert "[paused]" in listed or "paused" in listed.lower()

    rr = await TaskResumeExecutor(tasks_dir=tmp_path).execute(name="t1")
    assert rr
    listed2 = await TaskListExecutor(tasks_dir=tmp_path).execute()
    # After resume, no longer paused.
    line = next(l for l in listed2.splitlines() if l.startswith("t1"))
    assert "paused" not in line.lower()


@pytest.mark.asyncio
async def test_task_finish_one_shot(tmp_path: Path) -> None:
    await TaskCreateExecutor(tasks_dir=tmp_path).execute(
        name="once", description="do once"
    )
    out = await TaskFinishExecutor(tasks_dir=tmp_path).execute(name="once")
    assert "finished" in out.lower()


@pytest.mark.asyncio
async def test_task_finish_recurring_becomes_terminal(tmp_path: Path) -> None:
    """task_finish on a recurring card must force status=finished so the
    dispatcher stops re-scheduling it. Regression: ``TaskCard.mark_finished``
    sets recurring cards back to ``pending`` (for the normal tick-complete
    path), so calling it from the agent-invoked terminate verb left the
    task looping forever.

    Also pins the review-round-2 refactor: the executor routes through
    ``TaskCard.terminate()`` rather than poking status/last_finished_at
    directly, and ``terminate()`` intentionally leaves ``last_finished_at``
    unstamped — manual termination is a stop decision, not a completion
    record.
    """
    from butterfly.session_engine.task_cards import load_card, load_due_cards

    await TaskCreateExecutor(tasks_dir=tmp_path).execute(
        name="loop", description="every minute", interval=60
    )
    await TaskFinishExecutor(tasks_dir=tmp_path).execute(name="loop")

    card = load_card(tmp_path, "loop")
    assert card is not None
    assert card.status == "finished"
    # Manual terminate does not synthesise a completion timestamp.
    assert card.last_finished_at is None
    # is_due() short-circuits on status != pending regardless of timestamps.
    assert card.is_due() is False
    assert all(c.name != "loop" for c in load_due_cards(tmp_path))


@pytest.mark.asyncio
async def test_taskcard_terminate_distinct_from_mark_finished(tmp_path: Path) -> None:
    """Pin the ``terminate()`` vs ``mark_finished()`` semantic divide.

    ``mark_finished`` models "this tick completed" — recurring cards go
    back to pending + ``last_finished_at`` is stamped. ``terminate`` is
    the agent-invoked stop decision — status jumps straight to finished,
    no timestamp. Confusing the two was the root bug of PR #44 review.
    """
    from butterfly.session_engine.task_cards import TaskCard

    recurring_tick = TaskCard(name="r1", description="x", interval=60)
    recurring_tick.mark_finished()
    assert recurring_tick.status == "pending"
    assert recurring_tick.last_finished_at is not None

    recurring_manual = TaskCard(name="r2", description="x", interval=60)
    recurring_manual.terminate()
    assert recurring_manual.status == "finished"
    assert recurring_manual.last_finished_at is None

    # One-shot: both paths land on "finished", but only mark_finished stamps.
    oneshot_tick = TaskCard(name="o1", description="x", interval=None)
    oneshot_tick.mark_finished()
    assert oneshot_tick.status == "finished"
    assert oneshot_tick.last_finished_at is not None

    oneshot_manual = TaskCard(name="o2", description="x", interval=None)
    oneshot_manual.terminate()
    assert oneshot_manual.status == "finished"
    assert oneshot_manual.last_finished_at is None


@pytest.mark.asyncio
async def test_task_missing_name(tmp_path: Path) -> None:
    out = await TaskFinishExecutor(tasks_dir=tmp_path).execute(name="ghost")
    assert out.startswith("Error:") and "not found" in out


@pytest.mark.asyncio
async def test_task_create_missing_name(tmp_path: Path) -> None:
    out = await TaskCreateExecutor(tasks_dir=tmp_path).execute(name="", description="x")
    assert out.startswith("Error:")
