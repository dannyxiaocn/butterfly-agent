"""PR #19 review coverage: agent `_execute_tools` routing of backgroundable calls.

- When `run_in_background=true` on a backgroundable tool AND a `background_spawn`
  is configured, the tool's real executor MUST NOT be called; a placeholder
  result with the `task_id` is returned instead.
- When `run_in_background=true` but no spawner is wired, the agent falls back
  to synchronous execution. Control-only kwargs (`run_in_background`,
  `polling_interval`) leak into the tool call in this path — today bash
  tolerates unknown kwargs but that's an implicit contract worth pinning.
"""
from __future__ import annotations

from typing import Any

import pytest

from butterfly.core.agent import _execute_tools
from butterfly.core.tool import Tool
from butterfly.core.types import ToolCall


class _RecordingFunc:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        return "sync-ok"


@pytest.mark.asyncio
async def test_backgroundable_call_with_spawner_returns_placeholder() -> None:
    rec = _RecordingFunc()
    tool = Tool(
        name="bash",
        description="test",
        func=rec,
        schema={"type": "object", "properties": {"command": {"type": "string"}}},
        backgroundable=True,
    )
    tool_map = {"bash": tool}
    spawn_seen: dict[str, Any] = {}

    async def fake_spawn(name: str, input: dict[str, Any], polling_interval) -> str:
        spawn_seen["name"] = name
        spawn_seen["input"] = input
        spawn_seen["polling"] = polling_interval
        return "bg_abc123"

    calls = [
        ToolCall(
            id="tc1",
            name="bash",
            input={"command": "echo hi", "run_in_background": True, "polling_interval": 7},
        )
    ]
    results = await _execute_tools(calls, tool_map, background_spawn=fake_spawn)

    # The synchronous executor is NOT called.
    assert rec.calls == []
    # Placeholder result contains the task_id and fetch instruction.
    assert len(results) == 1
    result = results[0]
    assert result["is_error"] is False
    assert "bg_abc123" in result["content"]
    assert "tool_output" in result["content"]
    # Spawner receives the bg input without control flags.
    assert spawn_seen["name"] == "bash"
    assert spawn_seen["input"] == {"command": "echo hi"}
    assert spawn_seen["polling"] == 7


@pytest.mark.asyncio
async def test_backgroundable_call_without_spawner_falls_through() -> None:
    """Cubic P2 (fixed): when no spawner is wired, the agent layer strips
    `run_in_background` and `polling_interval` before calling the tool
    executor. Previously those flags leaked through and only survived
    because bash accepts `**kwargs`; a backgroundable tool with an explicit
    signature would have raised TypeError. This pins the corrected contract.
    """
    rec = _RecordingFunc()
    tool = Tool(
        name="bash",
        description="test",
        func=rec,
        schema={"type": "object", "properties": {"command": {"type": "string"}}},
        backgroundable=True,
    )
    calls = [
        ToolCall(
            id="tc1",
            name="bash",
            input={"command": "echo hi", "run_in_background": True, "polling_interval": 5},
        )
    ]
    results = await _execute_tools(calls, {"bash": tool}, background_spawn=None)
    # Executor WAS called (sync fallback).
    assert len(rec.calls) == 1
    # Control flags are stripped before reaching the tool executor.
    assert "run_in_background" not in rec.calls[0]
    assert "polling_interval" not in rec.calls[0]
    assert rec.calls[0] == {"command": "echo hi"}
    assert results[0]["content"] == "sync-ok"


@pytest.mark.asyncio
async def test_non_backgroundable_flag_is_ignored() -> None:
    """If a tool is not backgroundable, run_in_background is a no-op routing-wise."""
    rec = _RecordingFunc()
    tool = Tool(
        name="read",
        description="test",
        func=rec,
        schema={"type": "object", "properties": {}},
        backgroundable=False,
    )

    async def fake_spawn(*a, **kw):
        raise AssertionError("spawn must not be called for non-backgroundable tools")

    calls = [
        ToolCall(
            id="tc1",
            name="read",
            input={"path": "f.txt", "run_in_background": True},
        )
    ]
    results = await _execute_tools(calls, {"read": tool}, background_spawn=fake_spawn)
    assert len(rec.calls) == 1
    assert results[0]["is_error"] is False


@pytest.mark.asyncio
async def test_unknown_tool_returns_error() -> None:
    calls = [ToolCall(id="tc1", name="ghost", input={})]
    results = await _execute_tools(calls, {}, background_spawn=None)
    assert results[0]["is_error"] is True
    assert "not found" in results[0]["content"]
