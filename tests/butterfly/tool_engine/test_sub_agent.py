"""sub_agent — tool schema + sync executor + runner contract.

End-to-end runs against a real child session would need an LLM provider;
that's covered by manual smoke tests on the merged branch. Here we cover
the structural contract: schema shape, error paths, runner validate, and
helper behavior.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from butterfly.tool_engine.sub_agent import (
    SubAgentRunner,
    SubAgentTool,
    _compose_initial_message,
    _validate_mode,
)


_TOOL_JSON = Path(__file__).resolve().parent.parent.parent.parent / "toolhub" / "sub_agent" / "tool.json"


def test_tool_json_declares_backgroundable_with_required_inputs() -> None:
    schema = json.loads(_TOOL_JSON.read_text(encoding="utf-8"))
    assert schema["name"] == "sub_agent"
    assert schema["backgroundable"] is True
    assert "task" in schema["input_schema"]["properties"]
    assert "mode" in schema["input_schema"]["properties"]
    # Mode must be enum-restricted to the two known modes.
    assert set(schema["input_schema"]["properties"]["mode"]["enum"]) == {"explorer", "executor"}
    # Description must call out the "only final reply is forwarded" contract
    # so the LLM isn't surprised when intermediate child events don't appear.
    assert "final reply" in schema["description"].lower()


def test_validate_mode_accepts_known_modes() -> None:
    _validate_mode("explorer")
    _validate_mode("executor")
    with pytest.raises(ValueError):
        _validate_mode("overlord")
    with pytest.raises(ValueError):
        _validate_mode(None)


def test_compose_initial_message_includes_mode_marker() -> None:
    msg = _compose_initial_message("do thing X", "explorer")
    assert "explorer" in msg.lower()
    assert "do thing X" in msg
    assert "[DONE]" in msg or "DONE" in msg


@pytest.mark.asyncio
async def test_sub_agent_tool_rejects_call_without_parent_context() -> None:
    tool = SubAgentTool()  # no parent_session_id
    out = await tool.execute(task="x", mode="explorer")
    assert out.startswith("Error:")
    assert "parent" in out.lower()


@pytest.mark.asyncio
async def test_sub_agent_tool_rejects_invalid_mode() -> None:
    tool = SubAgentTool(parent_session_id="parent")
    out = await tool.execute(task="x", mode="overlord")
    assert out.startswith("Error:")
    assert "mode" in out


def test_runner_validate_requires_task_and_mode() -> None:
    runner = SubAgentRunner(
        parent_session_id="p",
        sessions_base=Path("/tmp"),
        system_sessions_base=Path("/tmp"),
        agent_base=Path("/tmp"),
    )
    with pytest.raises(ValueError, match="task is required"):
        runner.validate({"mode": "explorer"})
    with pytest.raises(ValueError, match="mode"):
        runner.validate({"task": "x"})
    runner.validate({"task": "x", "mode": "explorer"})  # ok
