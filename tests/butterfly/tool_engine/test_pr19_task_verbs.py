"""Smoke coverage for the task_* verb tools (task_create / _finish / _pause /
_resume / _list / _update) after the v2.0.29 single-script rewrite.
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
async def test_task_create_requires_script(tmp_path: Path) -> None:
    out = await TaskCreateExecutor(tasks_dir=tmp_path).execute(
        name="demo", description="say hi", check_interval=60
    )
    assert out.startswith("Error:") and "script" in out


@pytest.mark.asyncio
async def test_task_verbs_invoke_on_change_hook(tmp_path: Path) -> None:
    """v2.0.30 — every task_* tool must invoke the ``on_change`` callback
    after it successfully mutates a card. Session wires the callback to
    emit a ``task_card_changed`` event on events.jsonl; without the hook,
    the frontend Tasks tab would go stale until the next full refresh."""
    calls: list[tuple[str, str]] = []
    on_change = lambda name, change: calls.append((name, change))

    create = TaskCreateExecutor(tasks_dir=tmp_path, on_change=on_change)
    await create.execute(
        name="demo", description="say hi", check_interval=60,
        script="echo [start]",
    )
    assert calls[-1] == ("demo", "created")

    from toolhub.task_update.executor import TaskUpdateExecutor
    update = TaskUpdateExecutor(tasks_dir=tmp_path, on_change=on_change)
    # v2.0.30: script is edited via old_string/new_string, mirroring the
    # `edit` tool. Initial script was "echo [start]" from the create call.
    await update.execute(
        name="demo", old_string="echo [start]", new_string="echo [skip]",
    )
    assert calls[-1] == ("demo", "updated")

    pause = TaskPauseExecutor(tasks_dir=tmp_path, on_change=on_change)
    await pause.execute(name="demo")
    assert calls[-1] == ("demo", "paused")

    resume = TaskResumeExecutor(tasks_dir=tmp_path, on_change=on_change)
    await resume.execute(name="demo")
    assert calls[-1] == ("demo", "resumed")

    finish = TaskFinishExecutor(tasks_dir=tmp_path, on_change=on_change)
    await finish.execute(name="demo")
    assert calls[-1] == ("demo", "finished")

    # The callback should fire exactly five times total.
    assert [c[1] for c in calls] == [
        "created", "updated", "paused", "resumed", "finished",
    ]


@pytest.mark.asyncio
async def test_task_create_and_list_roundtrip(tmp_path: Path) -> None:
    create = TaskCreateExecutor(tasks_dir=tmp_path)
    out = await create.execute(
        name="demo",
        description="say hi",
        check_interval=60,
        script="echo [start]",
    )
    assert "Created task 'demo'" in out
    # Duplicate is rejected.
    dup = await create.execute(
        name="demo",
        description="again",
        check_interval=60,
        script="echo [start]",
    )
    assert dup.startswith("Error:") and "already exists" in dup

    listed = await TaskListExecutor(tasks_dir=tmp_path).execute()
    assert "demo" in listed
    assert "60s" in listed
    assert "script=yes" in listed


@pytest.mark.asyncio
async def test_task_create_writes_script_on_disk(tmp_path: Path) -> None:
    await TaskCreateExecutor(tasks_dir=tmp_path).execute(
        name="build",
        description="watch build",
        check_interval=30,
        script='[[ -f /tmp/flag ]] && echo "[start] flag ready" || echo [skip]',
    )
    body = (tmp_path / "build.sh").read_text(encoding="utf-8")
    assert body.startswith("#!/bin/bash")
    assert "echo [skip]" in body


@pytest.mark.asyncio
async def test_task_create_supports_done_tag(tmp_path: Path) -> None:
    """v2.0.29: [done] is now a script-level retire signal — same script."""
    out = await TaskCreateExecutor(tasks_dir=tmp_path).execute(
        name="oneshot",
        description="self-retiring",
        check_interval=30,
        script='(( $(date +%s) >= 0 )) && echo [done] || echo [start]',
    )
    assert "Created task 'oneshot'" in out
    body = (tmp_path / "oneshot.sh").read_text(encoding="utf-8")
    assert "[done]" in body


@pytest.mark.asyncio
async def test_task_pause_resume_roundtrip(tmp_path: Path) -> None:
    await TaskCreateExecutor(tasks_dir=tmp_path).execute(
        name="t1",
        description="work",
        check_interval=10,
        script="echo [start]",
    )
    pr = await TaskPauseExecutor(tasks_dir=tmp_path).execute(name="t1")
    assert "paused" in pr.lower() or "t1" in pr
    listed = await TaskListExecutor(tasks_dir=tmp_path).execute()
    assert "[paused]" in listed

    rr = await TaskResumeExecutor(tasks_dir=tmp_path).execute(name="t1")
    assert rr
    listed2 = await TaskListExecutor(tasks_dir=tmp_path).execute()
    line = next(l for l in listed2.splitlines() if l.startswith("t1"))
    assert "paused" not in line.lower()


@pytest.mark.asyncio
async def test_task_finish_marks_finished(tmp_path: Path) -> None:
    from butterfly.session_engine.task_cards import load_card
    await TaskCreateExecutor(tasks_dir=tmp_path).execute(
        name="once",
        description="do once",
        check_interval=60,
        script="echo [start]",
    )
    out = await TaskFinishExecutor(tasks_dir=tmp_path).execute(name="once")
    assert "finished" in out.lower()
    # Reviewer pin (PR #45): task_finish must leave the card in the
    # sticky "finished" state, NOT the recurring "pending" state that
    # TaskCard.mark_finished() uses.
    card = load_card(tmp_path, "once")
    assert card is not None
    assert card.status == "finished"


@pytest.mark.asyncio
async def test_task_missing_name(tmp_path: Path) -> None:
    out = await TaskFinishExecutor(tasks_dir=tmp_path).execute(name="ghost")
    assert out.startswith("Error:") and "not found" in out


@pytest.mark.asyncio
async def test_task_create_missing_name(tmp_path: Path) -> None:
    out = await TaskCreateExecutor(tasks_dir=tmp_path).execute(
        name="", description="x", script="echo [start]"
    )
    assert out.startswith("Error:")


@pytest.mark.asyncio
async def test_task_update_script_uses_edit_semantics(tmp_path: Path) -> None:
    """v2.0.30 — task_update changes the bash body via ``old_string`` +
    ``new_string`` exactly the way the ``edit`` tool does. Tests the
    happy path + the uniqueness guard + the mandatory pair rule."""
    from toolhub.task_update.executor import TaskUpdateExecutor
    from butterfly.session_engine.task_cards import read_script

    await TaskCreateExecutor(tasks_dir=tmp_path).execute(
        name="t1", description="", check_interval=60,
        script="echo [skip]\n# placeholder\n",
    )
    update = TaskUpdateExecutor(tasks_dir=tmp_path)

    # Happy path: single replacement.
    out = await update.execute(
        name="t1", old_string="[skip]", new_string="[start]",
    )
    assert "Updated task 't1'" in out
    assert "echo [start]" in read_script(tmp_path, "t1")

    # old_string not present → error.
    out = await update.execute(
        name="t1", old_string="does-not-exist", new_string="x",
    )
    assert out.startswith("Error:") and "not found" in out

    # Lone old_string without new_string → usage error.
    out = await update.execute(name="t1", old_string="foo")
    assert out.startswith("Error:") and "both" in out

    # Uniqueness guard: multiple matches without replace_all → error.
    await update.execute(name="t1", old_string="# placeholder", new_string="# dup\n# dup")
    out = await update.execute(name="t1", old_string="dup", new_string="triple")
    assert out.startswith("Error:") and "2 times" in out

    # replace_all=true → all matches replaced.
    out = await update.execute(
        name="t1", old_string="dup", new_string="triple", replace_all=True,
    )
    assert "Updated task 't1'" in out
    assert "triple" in read_script(tmp_path, "t1")
    assert "dup" not in read_script(tmp_path, "t1")
