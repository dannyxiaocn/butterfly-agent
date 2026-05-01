"""BackgroundTaskManager — round-trip + events."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from butterfly.session_engine.panel import (
    STATUS_COMPLETED,
    STATUS_KILLED,
    STATUS_KILLED_BY_RESTART,
    STATUS_RUNNING,
    create_pending_tool_entry,
    load_entry,
    save_entry,
)
from butterfly.tool_engine.background import BackgroundEvent, BackgroundTaskManager


async def _drain_until_completed(
    mgr: BackgroundTaskManager, tid: str, timeout: float = 15.0
) -> list[BackgroundEvent]:
    """Collect events from mgr.events until we see a terminal one for `tid`."""
    events: list[BackgroundEvent] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            evt = await asyncio.wait_for(mgr.events.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        if evt.tid != tid:
            continue
        events.append(evt)
        if evt.kind == "completed":
            return events
    raise AssertionError(f"Never saw 'completed' event for {tid}; got {[e.kind for e in events]}")


@pytest.mark.skipif(sys.platform == "win32", reason="needs bash")
@pytest.mark.asyncio
async def test_background_spawn_completes_and_emits_event(tmp_path: Path) -> None:
    panel_dir = tmp_path / "panel"
    out_dir = tmp_path / "out"
    panel_dir.mkdir()
    out_dir.mkdir()

    mgr = BackgroundTaskManager(panel_dir=panel_dir, tool_results_dir=out_dir)
    tid = await mgr.spawn("bash", {"command": "echo background-hello"})
    assert tid.startswith("bg_")

    events = await _drain_until_completed(mgr, tid)
    completed = events[-1]
    assert completed.kind == "completed"
    assert completed.entry.status == STATUS_COMPLETED
    assert completed.entry.exit_code == 0

    # Output file contains the command output.
    output_path = out_dir / f"{tid}.txt"
    assert output_path.exists()
    assert "background-hello" in output_path.read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="needs bash")
@pytest.mark.asyncio
async def test_background_kill_stops_running_task(tmp_path: Path) -> None:
    panel_dir = tmp_path / "panel"
    out_dir = tmp_path / "out"
    panel_dir.mkdir()
    out_dir.mkdir()

    mgr = BackgroundTaskManager(panel_dir=panel_dir, tool_results_dir=out_dir)
    tid = await mgr.spawn("bash", {"command": "sleep 30"})

    # Give the subprocess a moment to register its PID on the entry.
    for _ in range(20):
        entry = load_entry(panel_dir, tid)
        if entry and entry.pid:
            break
        await asyncio.sleep(0.05)

    killed = await mgr.kill(tid)
    assert killed is True

    # Wait for the completion event.
    deadline = time.monotonic() + 10
    saw_terminal = False
    while time.monotonic() < deadline:
        try:
            evt = await asyncio.wait_for(mgr.events.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        if evt.tid != tid:
            continue
        if evt.kind == "completed":
            saw_terminal = True
            break
    assert saw_terminal
    final = load_entry(panel_dir, tid)
    assert final is not None
    # Killed-preempt path: entry is STATUS_KILLED (set by kill() before exit).
    assert final.status in (STATUS_KILLED, STATUS_COMPLETED)


@pytest.mark.skipif(sys.platform == "win32", reason="needs bash")
@pytest.mark.asyncio
async def test_background_kill_unknown_tid(tmp_path: Path) -> None:
    panel_dir = tmp_path / "panel"
    out_dir = tmp_path / "out"
    panel_dir.mkdir()
    out_dir.mkdir()
    mgr = BackgroundTaskManager(panel_dir=panel_dir, tool_results_dir=out_dir)
    assert (await mgr.kill("nope")) is False


@pytest.mark.skipif(sys.platform == "win32", reason="needs bash")
@pytest.mark.asyncio
async def test_background_spawn_requires_command(tmp_path: Path) -> None:
    panel_dir = tmp_path / "panel"
    out_dir = tmp_path / "out"
    panel_dir.mkdir()
    out_dir.mkdir()
    mgr = BackgroundTaskManager(panel_dir=panel_dir, tool_results_dir=out_dir)
    with pytest.raises(ValueError):
        await mgr.spawn("bash", {})


def test_sweep_restart_marks_running_entries(tmp_path: Path) -> None:
    panel_dir = tmp_path / "panel"
    out_dir = tmp_path / "out"
    panel_dir.mkdir()
    out_dir.mkdir()

    entry = create_pending_tool_entry(panel_dir, tool_name="bash", input={})
    assert entry.status == STATUS_RUNNING

    mgr = BackgroundTaskManager(panel_dir=panel_dir, tool_results_dir=out_dir)
    updated = mgr.sweep_restart()
    assert len(updated) == 1
    assert updated[0].tid == entry.tid
    assert updated[0].status == STATUS_KILLED_BY_RESTART

    # A matching event lands on the queue.
    evt = mgr.events.get_nowait()
    assert evt.tid == entry.tid
    assert evt.kind == "killed_by_restart"
