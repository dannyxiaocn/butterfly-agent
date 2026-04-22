"""subagent_list + subagent_resume — tool schemas and executor contracts."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from butterfly.session_engine.panel import (
    STATUS_COMPLETED,
    STATUS_RUNNING,
    TYPE_PENDING_TOOL,
    TYPE_SUB_AGENT,
    create_pending_tool_entry,
    save_entry,
)
from butterfly.tool_engine.subagent_list import SubAgentListTool
from butterfly.tool_engine.subagent_resume import SubAgentResumeTool


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TOOLHUB = _REPO_ROOT / "toolhub"


# ── Tool-json schemas ────────────────────────────────────────────────────────

def test_subagent_list_tool_json() -> None:
    schema = json.loads((_TOOLHUB / "subagent_list" / "tool.json").read_text(encoding="utf-8"))
    assert schema["name"] == "subagent_list"
    assert schema["input_schema"]["properties"] == {}


def test_subagent_resume_tool_json() -> None:
    schema = json.loads((_TOOLHUB / "subagent_resume" / "tool.json").read_text(encoding="utf-8"))
    assert schema["name"] == "subagent_resume"
    required = set(schema["input_schema"]["required"])
    assert required == {"name", "message"}
    assert "timeout_seconds" in schema["input_schema"]["properties"]


# ── subagent_list ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_returns_empty_message_when_no_children(tmp_path: Path) -> None:
    tool = SubAgentListTool(parent_session_id="p", sessions_base=tmp_path)
    out = await tool.execute()
    assert "No sub-agents yet" in out


@pytest.mark.asyncio
async def test_list_rejects_missing_parent_context() -> None:
    tool = SubAgentListTool()
    out = await tool.execute()
    assert out.startswith("Error:")
    assert "parent" in out.lower()


@pytest.mark.asyncio
async def test_list_enumerates_running_and_terminal(tmp_path: Path) -> None:
    panel_dir = tmp_path / "p" / "core" / "panel"
    running = create_pending_tool_entry(
        panel_dir, tool_name="subagent_new",
        input={"task": "audit", "mode": "explorer", "name": "audit-child"},
        entry_type=TYPE_SUB_AGENT,
        meta={"child_session_id": "s-audit", "display_name": "audit-child",
              "mode": "explorer", "agent": "agent"},
    )
    time.sleep(0.001)  # keep created_at order deterministic
    done = create_pending_tool_entry(
        panel_dir, tool_name="subagent_new",
        input={"task": "port", "mode": "executor", "name": "porter"},
        entry_type=TYPE_SUB_AGENT,
        meta={"child_session_id": "s-port", "display_name": "porter",
              "mode": "executor", "agent": "agent"},
    )
    done.status = STATUS_COMPLETED
    save_entry(panel_dir, done)

    tool = SubAgentListTool(parent_session_id="p", sessions_base=tmp_path)
    out = await tool.execute()
    assert "2 sub-agent(s)" in out
    assert "1 running" in out and "1 finished" in out
    assert "audit-child" in out and "s-audit" in out
    assert "porter" in out and "s-port" in out
    # Oldest-first: running listed before done.
    assert out.index("audit-child") < out.index("porter")
    # Surface the resume hint so the LLM knows the follow-up tool.
    assert "subagent_resume" in out
    # running status preserved verbatim.
    assert STATUS_RUNNING in out


# ── subagent_resume ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resume_rejects_missing_parent_context() -> None:
    tool = SubAgentResumeTool()
    out = await tool.execute(name="x", message="hi")
    assert out.startswith("Error:")
    assert "parent" in out.lower()


@pytest.mark.asyncio
async def test_resume_missing_args_error(tmp_path: Path) -> None:
    tool = SubAgentResumeTool(parent_session_id="p", sessions_base=tmp_path)
    out = await tool.execute(name="x")
    assert out.startswith("Error:") and "message" in out
    out = await tool.execute(message="hi")
    assert out.startswith("Error:") and "name" in out
    out = await tool.execute(name="x", message="   ")
    assert out.startswith("Error:") and "non-empty" in out


@pytest.mark.asyncio
async def test_resume_unknown_name_lists_known(tmp_path: Path) -> None:
    panel_dir = tmp_path / "p" / "core" / "panel"
    create_pending_tool_entry(
        panel_dir, tool_name="subagent_new",
        input={"task": "t", "mode": "explorer", "name": "alpha"},
        entry_type=TYPE_SUB_AGENT,
        meta={"child_session_id": "s-alpha", "display_name": "alpha",
              "mode": "explorer", "agent": "a"},
    )
    tool = SubAgentResumeTool(parent_session_id="p", sessions_base=tmp_path)
    out = await tool.execute(name="beta", message="hi there")
    assert out.startswith("Error:")
    assert "'beta'" in out
    assert "'alpha'" in out  # hints the known names


@pytest.mark.asyncio
async def test_resume_unknown_name_when_no_children(tmp_path: Path) -> None:
    tool = SubAgentResumeTool(parent_session_id="p", sessions_base=tmp_path)
    out = await tool.execute(name="ghost", message="hi")
    assert "subagent_new" in out


@pytest.mark.asyncio
async def test_resume_picks_most_recent_on_duplicate_name(tmp_path: Path) -> None:
    """When two children share a display_name, resume talks to the newest one."""
    panel_dir = tmp_path / "p" / "core" / "panel"
    create_pending_tool_entry(
        panel_dir, tool_name="subagent_new",
        input={"task": "t1", "mode": "explorer", "name": "dup"},
        entry_type=TYPE_SUB_AGENT,
        meta={"child_session_id": "s-old", "display_name": "dup",
              "mode": "explorer", "agent": "a"},
    )
    time.sleep(0.002)
    create_pending_tool_entry(
        panel_dir, tool_name="subagent_new",
        input={"task": "t2", "mode": "executor", "name": "dup"},
        entry_type=TYPE_SUB_AGENT,
        meta={"child_session_id": "s-new", "display_name": "dup",
              "mode": "executor", "agent": "a"},
    )

    sys_base = tmp_path / "_sessions"
    (sys_base / "s-new").mkdir(parents=True)
    (sys_base / "s-new" / "manifest.json").write_text(
        json.dumps({"session_id": "s-new", "agent": "a"}), encoding="utf-8",
    )
    tool = SubAgentResumeTool(
        parent_session_id="p",
        sessions_base=tmp_path,
        system_sessions_base=sys_base,
    )
    # Timeout immediately — we only care that the write landed in s-new not s-old.
    out = await tool.execute(name="dup", message="pickme", timeout_seconds=30)
    assert "timed out" in out.lower()
    ctx_new = (sys_base / "s-new" / "context.jsonl").read_text(encoding="utf-8")
    assert "pickme" in ctx_new
    # Old child MUST NOT receive the message.
    old_ctx = sys_base / "s-old" / "context.jsonl"
    assert not old_ctx.exists()


@pytest.mark.asyncio
async def test_resume_delivers_message_and_returns_reply(tmp_path: Path) -> None:
    """Happy path: simulate the child writing a matching turn to context.jsonl."""
    panel_dir = tmp_path / "p" / "core" / "panel"
    create_pending_tool_entry(
        panel_dir, tool_name="subagent_new",
        input={"task": "t", "mode": "explorer", "name": "helper"},
        entry_type=TYPE_SUB_AGENT,
        meta={"child_session_id": "s-helper", "display_name": "helper",
              "mode": "explorer", "agent": "a"},
    )
    sys_base = tmp_path / "_sessions"
    (sys_base / "s-helper").mkdir(parents=True)
    (sys_base / "s-helper" / "manifest.json").write_text(
        json.dumps({"session_id": "s-helper", "agent": "a"}), encoding="utf-8",
    )
    ctx_path = sys_base / "s-helper" / "context.jsonl"

    tool = SubAgentResumeTool(
        parent_session_id="p",
        sessions_base=tmp_path,
        system_sessions_base=sys_base,
    )

    async def _fake_child() -> None:
        # Wait for the tool to post the user_input, then write a matching turn.
        for _ in range(40):
            if ctx_path.exists():
                lines = ctx_path.read_text(encoding="utf-8").splitlines()
                for line in lines:
                    evt = json.loads(line)
                    if evt.get("type") == "user_input":
                        turn = {
                            "type": "turn",
                            "user_input_id": evt["id"],
                            "messages": [
                                {"role": "assistant", "content": "here you go"},
                            ],
                        }
                        with ctx_path.open("a", encoding="utf-8") as f:
                            f.write(json.dumps(turn) + "\n")
                        return
            await asyncio.sleep(0.05)

    fake = asyncio.create_task(_fake_child())
    out = await tool.execute(name="helper", message="what's up?", timeout_seconds=30)
    await fake
    assert out == "here you go"


@pytest.mark.asyncio
async def test_resume_cascades_cancel_to_child(tmp_path: Path) -> None:
    """Parent cancel triggers BridgeSession.send_interrupt on the child."""
    panel_dir = tmp_path / "p" / "core" / "panel"
    create_pending_tool_entry(
        panel_dir, tool_name="subagent_new",
        input={"task": "t", "mode": "explorer", "name": "slow"},
        entry_type=TYPE_SUB_AGENT,
        meta={"child_session_id": "s-slow", "display_name": "slow",
              "mode": "explorer", "agent": "a"},
    )
    sys_base = tmp_path / "_sessions"
    (sys_base / "s-slow").mkdir(parents=True)
    (sys_base / "s-slow" / "manifest.json").write_text(
        json.dumps({"session_id": "s-slow", "agent": "a"}), encoding="utf-8",
    )
    tool = SubAgentResumeTool(
        parent_session_id="p",
        sessions_base=tmp_path,
        system_sessions_base=sys_base,
    )

    task = asyncio.create_task(tool.execute(name="slow", message="hi", timeout_seconds=60))
    # Give the tool a moment to post the user_input and start polling.
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    events_path = sys_base / "s-slow" / "events.jsonl"
    assert events_path.exists()
    body = events_path.read_text(encoding="utf-8")
    assert '"type": "interrupt"' in body


# ── Additional coverage (PR #52 review follow-ups) ───────────────────────────

@pytest.mark.asyncio
async def test_list_filters_out_non_sub_agent_entries(tmp_path: Path) -> None:
    """subagent_list must skip non-sub-agent panel entries (e.g. background bash).

    The parent's panel dir holds every backgroundable tool's pending entry —
    bash runs, sub-agents, future tool types. subagent_list keys on
    TYPE_SUB_AGENT so the LLM only sees child sessions, not unrelated
    background work.
    """
    panel_dir = tmp_path / "p" / "core" / "panel"
    create_pending_tool_entry(
        panel_dir, tool_name="subagent_new",
        input={"task": "t", "mode": "explorer", "name": "child-one"},
        entry_type=TYPE_SUB_AGENT,
        meta={"child_session_id": "s-one", "display_name": "child-one",
              "mode": "explorer", "agent": "a"},
    )
    create_pending_tool_entry(
        panel_dir, tool_name="bash",
        input={"command": "sleep 30"},
        entry_type=TYPE_PENDING_TOOL,
    )
    tool = SubAgentListTool(parent_session_id="p", sessions_base=tmp_path)
    out = await tool.execute()
    assert "1 sub-agent(s)" in out
    assert "child-one" in out
    assert "bash" not in out and "sleep 30" not in out


@pytest.mark.asyncio
async def test_resume_rejects_entry_missing_child_session_id(tmp_path: Path) -> None:
    """Panel entry exists but ``meta.child_session_id`` is missing — tool
    must surface a ``corrupt panel entry`` error rather than silently
    timing out.

    This happens when the panel file on disk was written by an older
    version or hand-edited; without the guard the resume call would post
    to ``system_sessions_base/None/context.jsonl`` or similar.
    """
    panel_dir = tmp_path / "p" / "core" / "panel"
    create_pending_tool_entry(
        panel_dir, tool_name="subagent_new",
        input={"task": "t", "mode": "explorer", "name": "broken"},
        entry_type=TYPE_SUB_AGENT,
        meta={"display_name": "broken", "mode": "explorer", "agent": "a"},
    )
    tool = SubAgentResumeTool(parent_session_id="p", sessions_base=tmp_path)
    out = await tool.execute(name="broken", message="hi")
    assert out.startswith("Error:")
    assert "child_session_id" in out
    # Guide the LLM toward the recovery action.
    assert "subagent_new" in out


@pytest.mark.asyncio
async def test_resume_auto_restarts_stopped_child(tmp_path: Path) -> None:
    """A child session in ``status=stopped`` must be flipped to active
    before the user_input posts — otherwise the follow-up would sit idle
    on disk until someone manually resumed the session.

    Verified by seeding ``status.json`` as stopped, running resume, then
    cancelling before the reply timeout: the post-cancel status.json must
    read ``active``.
    """
    panel_dir = tmp_path / "p" / "core" / "panel"
    create_pending_tool_entry(
        panel_dir, tool_name="subagent_new",
        input={"task": "t", "mode": "explorer", "name": "asleep"},
        entry_type=TYPE_SUB_AGENT,
        meta={"child_session_id": "s-asleep", "display_name": "asleep",
              "mode": "explorer", "agent": "a"},
    )
    sys_base = tmp_path / "_sessions"
    (sys_base / "s-asleep").mkdir(parents=True)
    (sys_base / "s-asleep" / "manifest.json").write_text(
        json.dumps({"session_id": "s-asleep", "agent": "a"}), encoding="utf-8",
    )
    (sys_base / "s-asleep" / "status.json").write_text(
        json.dumps({"status": "stopped", "stopped_at": "2026-04-21T00:00:00"}),
        encoding="utf-8",
    )

    tool = SubAgentResumeTool(
        parent_session_id="p",
        sessions_base=tmp_path,
        system_sessions_base=sys_base,
    )
    task = asyncio.create_task(
        tool.execute(name="asleep", message="wake up", timeout_seconds=60)
    )
    # Give the tool a moment to run _ensure_child_active + post the input.
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    status = json.loads(
        (sys_base / "s-asleep" / "status.json").read_text(encoding="utf-8")
    )
    assert status["status"] == "active", (
        f"_ensure_child_active failed to un-stop the child; status.json = {status!r}"
    )
    ctx = (sys_base / "s-asleep" / "context.jsonl").read_text(encoding="utf-8")
    assert "wake up" in ctx
