import json
import asyncio

import pytest

from nutshell.core.provider import Provider
from nutshell.core.agent import Agent
from nutshell.runtime.ipc import FileIPC, _context_event_to_display, _runtime_event_to_display
from nutshell.runtime.session import Session
from nutshell.runtime.status import read_session_status


class MockProvider(Provider):
    def __init__(self, responses):
        self._responses = iter(responses)

    async def complete(self, messages, tools, system_prompt, model, *, on_text_chunk=None, cache_system_prefix="", cache_last_human_turn=False, thinking: bool = False, thinking_budget: int = 8000, thinking_effort: str = "high"):
        from nutshell.core.types import TokenUsage
        r = next(self._responses)
        return (r[0], r[1], r[2] if len(r) > 2 else TokenUsage())


def make_session(tmp_path, agent, session_id="demo"):
    """Create a Session with the new layout (sessions/ + _sessions/)."""
    system_base = tmp_path / "_sessions"
    session = Session(agent=agent, session_id=session_id, base_dir=tmp_path, system_base=system_base)
    # Pre-populate core/ prompt files
    (session.core_dir / "system.md").write_text(agent.system_prompt or "", encoding="utf-8")
    (session.core_dir / "heartbeat.md").write_text(
        getattr(agent, "heartbeat_prompt", "") or "", encoding="utf-8"
    )
    (session.core_dir / "session.md").write_text(
        getattr(agent, "session_context_template", "") or "", encoding="utf-8"
    )
    return session


def test_context_event_to_display_expands_turn():
    """turn events are expanded into heartbeat_trigger + tool + agent display events."""
    turn = {
        "type": "turn",
        "triggered_by": "heartbeat",
        "ts": "2026-03-11T12:00:00",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "1", "name": "bash", "input": {"cmd": "ls"}},
                    {"type": "text", "text": "# Title\n\nbody"},
                ],
            }
        ],
    }

    # for_history=True: always emit heartbeat_trigger and tools
    events = _context_event_to_display(turn, for_history=True)
    assert events == [
        {"type": "heartbeat_trigger", "ts": "2026-03-11T12:00:00"},
        {"type": "tool", "name": "bash", "input": {"cmd": "ls"}, "ts": "2026-03-11T12:00:00"},
        {
            "type": "agent",
            "content": "# Title\n\nbody",
            "ts": "2026-03-11T12:00:00",
            "triggered_by": "heartbeat",
        },
    ]

    # for_history=False with pre_triggered=True: suppress heartbeat_trigger (already in events.jsonl)
    pre_triggered_turn = dict(turn, pre_triggered=True)
    sse_events = _context_event_to_display(pre_triggered_turn, for_history=False)
    assert sse_events[0]["type"] == "tool"  # no heartbeat_trigger at front

    # for_history=False with has_streaming_tools=True: suppress tools (already in events.jsonl)
    streamed_turn = dict(turn, has_streaming_tools=True)
    sse_events2 = _context_event_to_display(streamed_turn, for_history=False)
    assert not any(e["type"] == "tool" for e in sse_events2)


def test_context_event_to_display_passes_usage_to_agent():
    """usage field from turn event is forwarded to the agent display event."""
    turn = {
        "type": "turn",
        "triggered_by": "user",
        "ts": "2026-03-11T12:00:00",
        "usage": {"input": 150, "output": 42, "cache_read": 100, "cache_write": 0},
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
    }
    events = _context_event_to_display(turn, for_history=True)
    agent_events = [e for e in events if e["type"] == "agent"]
    assert len(agent_events) == 1
    assert agent_events[0]["usage"] == {"input": 150, "output": 42, "cache_read": 100, "cache_write": 0}


def test_context_event_to_display_no_usage_when_absent():
    """agent display event has no usage key when turn has no usage."""
    turn = {
        "type": "turn",
        "triggered_by": "user",
        "ts": "2026-03-11T12:00:00",
        "messages": [{"role": "assistant", "content": "hello"}],
    }
    events = _context_event_to_display(turn, for_history=True)
    agent_events = [e for e in events if e["type"] == "agent"]
    assert len(agent_events) == 1
    assert "usage" not in agent_events[0]


def test_runtime_event_to_display_passes_through():
    """model_status and other runtime events pass through unchanged."""
    model_status = {"type": "model_status", "state": "running", "source": "user", "ts": "2026-03-11T12:00:01"}
    assert _runtime_event_to_display(model_status) == [model_status]

    partial = {"type": "partial_text", "content": "hello", "ts": "T"}
    assert _runtime_event_to_display(partial) == [{"type": "partial_text", "content": "hello", "ts": "T"}]

    tool_call = {"type": "tool_call", "name": "bash", "input": {"cmd": "ls"}, "ts": "T"}
    assert _runtime_event_to_display(tool_call) == [
        {"type": "tool", "name": "bash", "input": {"cmd": "ls"}, "ts": "T"}
    ]


@pytest.mark.asyncio
async def test_session_chat_writes_turn_to_context_and_status_to_events(tmp_path):
    """chat() writes turn to context.jsonl and model_status to events.jsonl."""
    provider = MockProvider([("**done**", [])])
    agent = Agent(provider=provider)
    session = make_session(tmp_path, agent)
    ipc = FileIPC(session.system_dir)
    session._ipc = ipc

    await session.chat("hello")

    context_events = [
        json.loads(line)
        for line in session.system_dir.joinpath("context.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    runtime_events = [
        json.loads(line)
        for line in session.system_dir.joinpath("events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    # context.jsonl: only the turn
    assert [e["type"] for e in context_events] == ["turn"]

    # events.jsonl: model_status running + idle
    assert [e["type"] for e in runtime_events] == ["model_status", "model_status"]
    assert runtime_events[0]["state"] == "running"
    assert runtime_events[0]["source"] == "user"
    assert runtime_events[1]["state"] == "idle"
    assert runtime_events[1]["source"] == "user"

    status = read_session_status(session.system_dir)
    assert status["model_state"] == "idle"
    assert status["model_source"] == "user"


@pytest.mark.asyncio
async def test_session_chat_writes_idle_on_cancellation(tmp_path):
    """On cancellation, model_status(idle) is written to events.jsonl; context.jsonl is empty."""
    class CancellingProvider(Provider):
        async def complete(self, messages, tools, system_prompt, model, *, on_text_chunk=None, cache_system_prefix="", cache_last_human_turn=False, thinking: bool = False, thinking_budget: int = 8000, thinking_effort: str = "high"):
            raise asyncio.CancelledError()

    agent = Agent(provider=CancellingProvider())
    session = make_session(tmp_path, agent)
    ipc = FileIPC(session.system_dir)
    session._ipc = ipc

    with pytest.raises(asyncio.CancelledError):
        await session.chat("hello")

    context_events = [
        json.loads(line)
        for line in session.system_dir.joinpath("context.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    runtime_events = [
        json.loads(line)
        for line in session.system_dir.joinpath("events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    # context.jsonl: empty (no completed turn)
    assert context_events == []

    # events.jsonl: model_status running + idle
    assert [e["type"] for e in runtime_events] == ["model_status", "model_status"]
    assert runtime_events[0]["state"] == "running"
    assert runtime_events[1]["state"] == "idle"

    status = read_session_status(session.system_dir)
    assert status["model_state"] == "idle"
