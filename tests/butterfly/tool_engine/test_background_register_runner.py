"""BackgroundTaskManager — runner registration contract.

The v2.0.13 refactor split the manager (panel/event/lifecycle plumbing) from
per-tool runners. These tests lock down:
  - register_runner / runner_for round-trip
  - spawn raises when no runner is registered for the tool name
  - validate() runs synchronously before the panel entry is created
  - sub_agent panel entries get type=TYPE_SUB_AGENT
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from butterfly.session_engine.panel import (
    PanelEntry,
    TYPE_PENDING_TOOL,
    TYPE_SUB_AGENT,
    list_entries,
)
from butterfly.tool_engine.background import (
    BackgroundContext,
    BackgroundEvent,
    BackgroundTaskManager,
)


class _DummyRunner:
    def __init__(self):
        self.validated_with: list[dict] = []
        self.ran_with: list[dict] = []

    def validate(self, input: dict[str, Any]) -> None:
        self.validated_with.append(dict(input))
        if input.get("must_fail"):
            raise ValueError("dummy: invalid input")

    async def run(self, ctx, tid, entry, input, polling_interval):
        self.ran_with.append(dict(input))
        return 0

    async def kill(self, ctx, tid):
        return False


def test_register_runner_round_trip(tmp_path: Path) -> None:
    mgr = BackgroundTaskManager(panel_dir=tmp_path / "p", tool_results_dir=tmp_path / "r")
    runner = _DummyRunner()
    mgr.register_runner("dummy", runner)
    assert mgr.runner_for("dummy") is runner
    assert mgr.runner_for("nonexistent") is None
    # Bash is auto-registered.
    assert mgr.runner_for("bash") is not None


@pytest.mark.asyncio
async def test_spawn_unknown_tool_raises(tmp_path: Path) -> None:
    mgr = BackgroundTaskManager(panel_dir=tmp_path / "p", tool_results_dir=tmp_path / "r")
    with pytest.raises(ValueError, match="no runner registered"):
        await mgr.spawn("nope", {})


@pytest.mark.asyncio
async def test_spawn_calls_runner_validate_synchronously(tmp_path: Path) -> None:
    mgr = BackgroundTaskManager(panel_dir=tmp_path / "p", tool_results_dir=tmp_path / "r")
    runner = _DummyRunner()
    mgr.register_runner("dummy", runner)
    with pytest.raises(ValueError, match="dummy: invalid input"):
        await mgr.spawn("dummy", {"must_fail": True})
    # Failed validation MUST NOT create a panel entry.
    assert list_entries(tmp_path / "p") == []


@pytest.mark.asyncio
async def test_spawn_sub_agent_uses_sub_agent_panel_type(tmp_path: Path) -> None:
    mgr = BackgroundTaskManager(panel_dir=tmp_path / "p", tool_results_dir=tmp_path / "r")
    runner = _DummyRunner()
    mgr.register_runner("sub_agent", runner)
    tid = await mgr.spawn("sub_agent", {"task": "x", "mode": "explorer"})
    # Wait for the runner task to settle so the entry transitions terminally.
    for _ in range(40):
        if any(e.tid == tid and e.is_terminal() for e in list_entries(tmp_path / "p")):
            break
        await asyncio.sleep(0.05)
    entries = list_entries(tmp_path / "p")
    assert len(entries) == 1
    assert entries[0].type == TYPE_SUB_AGENT
    assert entries[0].tool_name == "sub_agent"


@pytest.mark.asyncio
async def test_spawn_other_tool_uses_pending_tool_type(tmp_path: Path) -> None:
    mgr = BackgroundTaskManager(panel_dir=tmp_path / "p", tool_results_dir=tmp_path / "r")
    runner = _DummyRunner()
    mgr.register_runner("dummy", runner)
    await mgr.spawn("dummy", {})
    for _ in range(40):
        entries = list_entries(tmp_path / "p")
        if entries and entries[0].is_terminal():
            break
        await asyncio.sleep(0.05)
    entries = list_entries(tmp_path / "p")
    assert entries[0].type == TYPE_PENDING_TOOL
