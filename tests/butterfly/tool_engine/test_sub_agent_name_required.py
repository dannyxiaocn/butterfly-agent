"""PR #37 / v2.0.22: required ``name`` parameter on sub_agent.

Pins the tool / runner contracts so future refactors don't silently drop
the required arg that keeps sibling sub-agents distinguishable in the
sidebar.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from butterfly.tool_engine.sub_agent import (
    SubAgentRunner,
    SubAgentTool,
    _validate_name,
)


def test_validate_name_trims_and_caps() -> None:
    assert _validate_name("  hello  ") == "hello"
    assert _validate_name("x" * 100) == "x" * 40


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "\t\n", 42, ["list"], {"dict": True}],
    ids=["none", "empty", "blank", "whitespace", "int", "list", "dict"],
)
def test_validate_name_rejects_bad_input(value) -> None:
    with pytest.raises(ValueError):
        _validate_name(value)


@pytest.mark.asyncio
async def test_sub_agent_tool_requires_name() -> None:
    tool = SubAgentTool(parent_session_id="parent")
    # Missing name keyword arg entirely.
    out = await tool.execute(task="x", mode="explorer")
    assert out.startswith("Error:")
    assert "name" in out
    # Blank name — surfaces the specific normalizer message.
    out = await tool.execute(task="x", mode="explorer", name="   ")
    assert "name must be a non-empty string" in out


def test_runner_validate_requires_name() -> None:
    runner = SubAgentRunner(
        parent_session_id="p",
        sessions_base=Path("."),
        system_sessions_base=Path("."),
        agent_base=Path("."),
    )
    with pytest.raises(ValueError):
        runner.validate({"task": "x", "mode": "explorer"})
    # Supplying a valid name + required fields should not raise.
    runner.validate({"task": "x", "mode": "explorer", "name": "ok"})


def test_spawn_child_writes_display_name_to_manifest(tmp_path: Path) -> None:
    """Name supplied to _spawn_child must land in the child's manifest.json."""
    from butterfly.tool_engine.sub_agent import _spawn_child

    sessions_base = tmp_path / "sessions"
    sys_base = tmp_path / "_sessions"
    agenthub = Path(__file__).resolve().parent.parent.parent.parent / "agenthub"
    sessions_base.mkdir()
    sys_base.mkdir()
    parent = "parent-a"
    (sys_base / parent).mkdir()
    (sys_base / parent / "manifest.json").write_text(
        json.dumps({"session_id": parent, "agent": "agent"}),
        encoding="utf-8",
    )
    child_id, _msg, _agt = _spawn_child(
        parent_session_id=parent,
        mode="explorer",
        task="do a thing",
        name="my-child-task",
        sessions_base=sessions_base,
        system_sessions_base=sys_base,
        agent_base=agenthub,
    )
    manifest = json.loads((sys_base / child_id / "manifest.json").read_text())
    assert manifest.get("display_name") == "my-child-task"
    assert manifest.get("parent_session_id") == parent


def test_spawn_child_truncates_long_display_name(tmp_path: Path) -> None:
    from butterfly.tool_engine.sub_agent import _spawn_child

    sessions_base = tmp_path / "sessions"
    sys_base = tmp_path / "_sessions"
    agenthub = Path(__file__).resolve().parent.parent.parent.parent / "agenthub"
    sessions_base.mkdir()
    sys_base.mkdir()
    parent = "parent-b"
    (sys_base / parent).mkdir()
    (sys_base / parent / "manifest.json").write_text(
        json.dumps({"session_id": parent, "agent": "agent"}),
        encoding="utf-8",
    )
    long_name = "Y" * 80
    child_id, _msg, _agt = _spawn_child(
        parent_session_id=parent,
        mode="explorer",
        task="do a thing",
        name=long_name,
        sessions_base=sessions_base,
        system_sessions_base=sys_base,
        agent_base=agenthub,
    )
    manifest = json.loads((sys_base / child_id / "manifest.json").read_text())
    assert len(manifest.get("display_name")) == 40
