import json
import asyncio

import pytest

from nutshell.core.agent import Agent
from nutshell.core.provider import Provider
from nutshell.core.tool import tool
from nutshell.core.types import ToolCall, TokenUsage
from nutshell.runtime.ipc import FileIPC, _context_event_to_display, _runtime_event_to_display
from nutshell.session_engine.session import Session
from nutshell.session_engine.session_status import read_session_status
from nutshell.session_engine.task_cards import TaskCard


class MockProvider(Provider):
    def __init__(self, responses):
        self._responses = iter(responses)

    async def complete(self, messages, tools, system_prompt, model, *, on_text_chunk=None, cache_system_prefix="", cache_last_human_turn=False, thinking: bool = False, thinking_budget: int = 8000, thinking_effort: str = "high"):
        r = next(self._responses)
        return (r[0], r[1], r[2] if len(r) > 2 else TokenUsage())


def read_jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def make_session(tmp_path, agent, session_id="demo", **kwargs):
    """Create a Session with the new layout (sessions/ + _sessions/)."""
    system_base = tmp_path / "_sessions"
    session = Session(agent=agent, session_id=session_id, base_dir=tmp_path, system_base=system_base, **kwargs)
    # Pre-populate core/ prompt files
    (session.core_dir / "system.md").write_text(agent.system_prompt or "", encoding="utf-8")
    (session.core_dir / "task.md").write_text("", encoding="utf-8")
    (session.core_dir / "env.md").write_text("", encoding="utf-8")
    return session


def test_context_event_to_display_expands_turn():
    """turn events are expanded into tool + agent display events."""
    turn = {
        "type": "turn",
        "triggered_by": "task:default",
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

    # for_history=True: always emit tools and agent text
    events = _context_event_to_display(turn, for_history=True)
    assert events == [
        {"type": "tool", "name": "bash", "input": {"cmd": "ls"}, "ts": "2026-03-11T12:00:00"},
        {
            "type": "agent",
            "content": "# Title\n\nbody",
            "ts": "2026-03-11T12:00:00",
        },
    ]

    # for_history=False with pre_triggered=True: tools still emitted
    pre_triggered_turn = dict(turn, pre_triggered=True)
    sse_events = _context_event_to_display(pre_triggered_turn, for_history=False)
    assert sse_events[0]["type"] == "tool"

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

    tool_done = {"type": "tool_done", "name": "bash", "result_len": 12, "ts": "T"}
    loop_start = {"type": "loop_start", "ts": "T"}
    loop_end = {"type": "loop_end", "iterations": 2, "usage": {"input": 1, "output": 2}, "ts": "T"}
    assert _runtime_event_to_display(tool_done) == [tool_done]
    assert _runtime_event_to_display(loop_start) == [loop_start]
    assert _runtime_event_to_display(loop_end) == [loop_end]


@pytest.mark.asyncio
async def test_session_chat_writes_turn_to_context_and_status_to_events(tmp_path):
    """chat() writes turn to context.jsonl and loop lifecycle events to events.jsonl."""
    provider = MockProvider([("**done**", [])])
    agent = Agent(provider=provider)
    session = make_session(tmp_path, agent)
    ipc = FileIPC(session.system_dir)
    session._ipc = ipc

    await session.chat("hello")

    context_events = read_jsonl(session.system_dir / "context.jsonl")
    runtime_events = read_jsonl(session.system_dir / "events.jsonl")

    # context.jsonl: only the turn
    assert [e["type"] for e in context_events] == ["turn"]

    # events.jsonl: model_status running + loop callbacks + idle
    assert [e["type"] for e in runtime_events] == ["model_status", "loop_start", "loop_end", "model_status"]
    assert runtime_events[0]["state"] == "running"
    assert runtime_events[0]["source"] == "user"
    assert runtime_events[2]["iterations"] == 1
    assert runtime_events[3]["state"] == "idle"
    assert runtime_events[3]["source"] == "user"

    status = read_session_status(session.system_dir)
    assert status["model_state"] == "idle"
    assert status["model_source"] == "user"


@pytest.mark.asyncio
async def test_session_chat_writes_idle_on_cancellation(tmp_path):
    """On cancellation, loop_start is recorded but loop_end is skipped; context stays empty."""
    class CancellingProvider(Provider):
        async def complete(self, messages, tools, system_prompt, model, *, on_text_chunk=None, cache_system_prefix="", cache_last_human_turn=False, thinking: bool = False, thinking_budget: int = 8000, thinking_effort: str = "high"):
            raise asyncio.CancelledError()

    agent = Agent(provider=CancellingProvider())
    session = make_session(tmp_path, agent)
    ipc = FileIPC(session.system_dir)
    session._ipc = ipc

    with pytest.raises(asyncio.CancelledError):
        await session.chat("hello")

    context_events = read_jsonl(session.system_dir / "context.jsonl")
    runtime_events = read_jsonl(session.system_dir / "events.jsonl")

    # context.jsonl: empty (no completed turn)
    assert context_events == []

    # events.jsonl: model_status running + loop_start + idle
    assert [e["type"] for e in runtime_events] == ["model_status", "loop_start", "model_status"]
    assert runtime_events[0]["state"] == "running"
    assert runtime_events[2]["state"] == "idle"

    status = read_session_status(session.system_dir)
    assert status["model_state"] == "idle"


@pytest.mark.asyncio
async def test_session_chat_composes_hook_events_with_external_callbacks(tmp_path):
    """chat() emits tool/loop events and still invokes external hook callbacks."""
    provider = MockProvider(
        [
            (
                "planning",
                [ToolCall(id="tc-1", name="echo_tool", input={"text": "ping"})],
                TokenUsage(input_tokens=10, output_tokens=1),
            ),
            ("done", [], TokenUsage(input_tokens=5, output_tokens=2, cache_read_tokens=3)),
        ]
    )

    @tool(description="Echo a value")
    async def echo_tool(text: str) -> str:
        return f"echo:{text}"

    starts: list[str] = []
    done_calls: list[tuple[str, dict, str]] = []
    ends: list[tuple[int, dict]] = []

    agent = Agent(provider=provider, tools=[echo_tool])
    session = make_session(
        tmp_path,
        agent,
        on_loop_start=starts.append,
        on_tool_done=lambda name, input, result: done_calls.append((name, input, result)),
        on_loop_end=lambda result: ends.append((result.iterations, result.usage.as_dict())),
    )
    session._load_session_capabilities = lambda: None
    session._ipc = FileIPC(session.system_dir)

    result = await session.chat("hello")

    runtime_events = read_jsonl(session.system_dir / "events.jsonl")
    context_events = read_jsonl(session.system_dir / "context.jsonl")

    assert result.iterations == 2
    assert [e["type"] for e in runtime_events] == [
        "model_status",
        "loop_start",
        "tool_call",
        "tool_done",
        "loop_end",
        "model_status",
    ]
    assert runtime_events[3]["name"] == "echo_tool"
    assert runtime_events[3]["result_len"] == 9
    assert runtime_events[4]["iterations"] == 2
    assert runtime_events[4]["usage"] == {"input": 15, "output": 3, "cache_read": 3, "cache_write": 0}

    assert starts == ["hello"]
    assert done_calls == [("echo_tool", {"text": "ping"}, "echo:ping")]
    assert ends == [(2, {"input": 15, "output": 3, "cache_read": 3, "cache_write": 0})]

    assert context_events[0]["has_streaming_tools"] is True
    assert context_events[0]["usage"] == {"input": 15, "output": 3, "cache_read": 3, "cache_write": 0}


@pytest.mark.asyncio
async def test_session_tick_emits_hook_events_and_preserves_turn_flags(tmp_path):
    """tick() streams task trigger, tool lifecycle, and pre-trigger metadata together."""
    provider = MockProvider(
        [
            (
                "planning",
                [ToolCall(id="tc-1", name="echo_tool", input={"text": "beat"})],
                TokenUsage(input_tokens=4, output_tokens=1),
            ),
            ("task done", [], TokenUsage(input_tokens=6, output_tokens=3)),
        ]
    )

    @tool(description="Echo a value")
    async def echo_tool(text: str) -> str:
        return f"echo:{text}"

    agent = Agent(provider=provider, tools=[echo_tool])
    session = make_session(tmp_path, agent)
    session._load_session_capabilities = lambda: None
    session._ipc = FileIPC(session.system_dir)

    result = await session.tick(TaskCard(name="duty", description="stay alive", interval=60))

    runtime_events = read_jsonl(session.system_dir / "events.jsonl")
    context_events = read_jsonl(session.system_dir / "context.jsonl")

    assert result is not None
    assert result.iterations == 2
    assert [e["type"] for e in runtime_events] == [
        "task_wakeup",
        "model_status",
        "loop_start",
        "tool_call",
        "tool_done",
        "loop_end",
        "model_status",
    ]
    assert runtime_events[5]["iterations"] == 2

    assert context_events[0]["triggered_by"] == "task:duty"
    assert context_events[0]["pre_triggered"] is True
    assert context_events[0]["has_streaming_tools"] is True
    assert context_events[0]["usage"] == {"input": 10, "output": 4, "cache_read": 0, "cache_write": 0}
