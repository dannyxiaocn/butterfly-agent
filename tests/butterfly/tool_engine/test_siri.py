"""siri — tool schema + sync executor + runner contract.

End-to-end runs against a real child session would need an LLM provider;
those are covered by manual smoke tests on the merged branch. Here we
cover the structural contract: schema shape, error paths, runner
validate, and the rewrite-into-sub_agent helper that powers both the
sync executor and the background runner.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from butterfly.tool_engine.siri import (
    SiriExecutor,
    SiriRunner,
    _AGENT_NAME,
    _MODE,
    _compose_task,
    _default_name,
    _validate_request,
)


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TOOL_JSON = _REPO_ROOT / "toolhub" / "siri" / "tool.json"
_AGENT_DIR = _REPO_ROOT / "agenthub" / "tool_agent"


# ── Tool schema ─────────────────────────────────────────────────────────────


def test_tool_json_declares_backgroundable_with_required_request() -> None:
    schema = json.loads(_TOOL_JSON.read_text(encoding="utf-8"))
    assert schema["name"] == "siri"
    assert schema["backgroundable"] is True
    props = schema["input_schema"]["properties"]
    assert "request" in props
    assert schema["input_schema"]["required"] == ["request"]
    # Description has to mention what the LLM-visible interface is and
    # that the executor delegates to a weak model — otherwise the parent
    # agent has no way to know what siri is for.
    desc = schema["description"].lower()
    assert "tool_agent" in desc or "weak" in desc
    assert "kimi-for-coding" in desc


# ── tool_agent definition ───────────────────────────────────────────────────


def test_tool_agent_pins_kimi_for_coding() -> None:
    cfg_path = _AGENT_DIR / "config.yaml"
    text = cfg_path.read_text(encoding="utf-8")
    # Light parse — we don't pull in PyYAML for one test. Both keys appear
    # on their own lines with the values we hard-wire siri against.
    assert "model: kimi-for-coding" in text
    assert "provider: kimi-coding-plan" in text
    assert "agent: tool_agent" in text


def test_tool_agent_does_not_recurse() -> None:
    """tool_agent must NOT list siri / subagent_new / workflow in its tool
    set — otherwise it could fork another sub-session and we lose the
    "leaf executor" contract the system prompt promises."""
    tools = (_AGENT_DIR / "tools.md").read_text(encoding="utf-8").splitlines()
    enabled = {t.strip() for t in tools if t.strip() and not t.strip().startswith("#")}
    forbidden = {"siri", "subagent_new", "workflow"}
    assert enabled.isdisjoint(forbidden), (
        f"tool_agent tools.md must not include {sorted(enabled & forbidden)} — "
        "tool_agent is the leaf executor of the siri chain."
    )


# ── helpers ─────────────────────────────────────────────────────────────────


def test_validate_request_rejects_empty_and_non_string() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _validate_request("   ")
    with pytest.raises(ValueError, match="string"):
        _validate_request(None)
    with pytest.raises(ValueError, match="string"):
        _validate_request(123)
    assert _validate_request("  hello  ") == "hello"


def test_default_name_collapses_whitespace_and_caps_length() -> None:
    name = _default_name("  do   thing  X  ")
    assert name == "do thing X"
    big = _default_name("x" * 200)
    assert len(big) <= 40
    assert _default_name("") == "siri"


def test_compose_task_includes_request_and_terminator_hint() -> None:
    out = _compose_task("grep TODO under src/")
    assert "grep TODO under src/" in out
    # The system-prompt contract for tool_agent includes [DONE] / [BLOCKED] /
    # [ERROR] terminators; the per-call wrapper restates them so the child
    # cannot miss them even if its system prompt is truncated.
    assert "[DONE]" in out
    assert "## Request" in out


# ── Sync executor ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_siri_executor_rejects_empty_request() -> None:
    tool = SiriExecutor(parent_session_id="parent-1")
    out = await tool.execute(request="   ")
    assert out.startswith("Error:")
    assert "request" in out


@pytest.mark.asyncio
async def test_siri_executor_rejects_missing_parent_context() -> None:
    # Without parent_session_id, SubAgentTool's own guard fires.
    tool = SiriExecutor()
    out = await tool.execute(request="anything")
    assert out.startswith("Error:")
    assert "parent" in out.lower()


class SiriExecutorRewriteTests(unittest.TestCase):
    """The sync executor must hand SubAgentTool exactly the right kwargs:
    forced ``agent_name=tool_agent`` + ``mode=executor`` + a default
    display name derived from the request."""

    def test_request_routed_to_tool_agent_in_executor_mode(self) -> None:
        captured: list[dict] = []

        async def fake_execute(self, **kwargs):
            captured.append(dict(kwargs))
            return "child-reply"

        tool = SiriExecutor(
            parent_session_id="parent-1",
            sessions_base=Path("/tmp/sessions"),
            system_sessions_base=Path("/tmp/_sessions"),
            agent_base=Path("/tmp/agenthub"),
        )
        with patch(
            "butterfly.tool_engine.sub_agent.SubAgentTool.execute",
            new=fake_execute,
        ):
            result = asyncio.run(tool.execute(request="grep TODO under src/"))

        self.assertEqual(result, "child-reply")
        self.assertEqual(len(captured), 1)
        kwargs = captured[0]
        self.assertEqual(kwargs["agent_name"], _AGENT_NAME)
        self.assertEqual(kwargs["mode"], _MODE)
        self.assertIn("grep TODO", kwargs["task"])
        # Display name defaulted from the request when not supplied.
        self.assertIn("grep", kwargs["name"].lower())

    def test_explicit_name_and_timeout_forwarded(self) -> None:
        captured: list[dict] = []

        async def fake_execute(self, **kwargs):
            captured.append(dict(kwargs))
            return "ok"

        tool = SiriExecutor(parent_session_id="parent-1")
        with patch(
            "butterfly.tool_engine.sub_agent.SubAgentTool.execute",
            new=fake_execute,
        ):
            asyncio.run(
                tool.execute(
                    request="ls /tmp",
                    name="list-tmp",
                    timeout_seconds=120,
                )
            )

        kwargs = captured[0]
        self.assertEqual(kwargs["name"], "list-tmp")
        self.assertEqual(kwargs["timeout_seconds"], 120)


# ── Background runner ──────────────────────────────────────────────────────


def test_runner_validate_requires_request() -> None:
    runner = SiriRunner(
        parent_session_id="p",
        sessions_base=Path("/tmp"),
        system_sessions_base=Path("/tmp"),
        agent_base=Path("/tmp"),
    )
    with pytest.raises(ValueError, match="request"):
        runner.validate({})
    with pytest.raises(ValueError, match="request"):
        runner.validate({"request": "   "})
    runner.validate({"request": "do something"})  # ok


def test_runner_rewrite_pins_agent_and_mode() -> None:
    rewritten = SiriRunner._rewrite({"request": "  do thing  "})
    assert rewritten["agent_name"] == _AGENT_NAME
    assert rewritten["mode"] == _MODE
    assert rewritten["name"] == "do thing"
    assert "do thing" in rewritten["task"]
    assert "timeout_seconds" not in rewritten

    with_timeout = SiriRunner._rewrite(
        {"request": "x", "name": "probe", "timeout_seconds": 90}
    )
    assert with_timeout["name"] == "probe"
    assert with_timeout["timeout_seconds"] == 90
