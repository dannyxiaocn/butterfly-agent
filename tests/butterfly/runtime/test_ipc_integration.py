import json
import asyncio

import pytest

from butterfly.core.agent import Agent
from butterfly.core.provider import Provider
from butterfly.core.tool import tool
from butterfly.core.types import ToolCall, TokenUsage
from butterfly.runtime.ipc import FileIPC, _context_event_to_display, _runtime_event_to_display
from butterfly.session_engine.session import Session
from butterfly.session_engine.session_status import read_session_status
from butterfly.session_engine.task_cards import TaskCard


class MockProvider(Provider):
    def __init__(self, responses):
        self._responses = iter(responses)

    async def complete(self, messages, tools, system_prompt, model, *, on_text_chunk=None, cache_system_prefix="", cache_last_human_turn=False, thinking: bool = False, thinking_budget: int = 8000, thinking_effort: str = "high", on_thinking_start=None, on_thinking_end=None):
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

    # for_history=True: always emit tools and agent text. v2.0.19 added an
    # `id` field propagated from the tool_use block (needed so history replay
    # can pair tool_use with its matching tool_result).
    events = _context_event_to_display(turn, for_history=True)
    assert events == [
        {"type": "tool", "name": "bash", "input": {"cmd": "ls"}, "ts": "2026-03-11T12:00:00", "id": "1"},
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
        async def complete(self, messages, tools, system_prompt, model, *, on_text_chunk=None, cache_system_prefix="", cache_last_human_turn=False, thinking: bool = False, thinking_budget: int = 8000, thinking_effort: str = "high", on_thinking_start=None, on_thinking_end=None):
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
    assert runtime_events[4]["usage"] == {"input": 15, "output": 3, "cache_read": 3, "cache_write": 0, "reasoning": 0}

    assert starts == ["hello"]
    assert done_calls == [("echo_tool", {"text": "ping"}, "echo:ping")]
    assert ends == [(2, {"input": 15, "output": 3, "cache_read": 3, "cache_write": 0, "reasoning": 0})]

    assert context_events[0]["has_streaming_tools"] is True
    assert context_events[0]["usage"] == {"input": 15, "output": 3, "cache_read": 3, "cache_write": 0, "reasoning": 0}


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
    assert context_events[0]["usage"] == {"input": 10, "output": 4, "cache_read": 0, "cache_write": 0, "reasoning": 0}


# ── v2.0.9 thinking cell — provider → IPC routing ────────────────────────────

class ThinkingProvider(Provider):
    """Mock provider that emits thinking lifecycle callbacks and plain text."""

    def __init__(self, thinking_bodies: list[str], text: str = "done"):
        self._bodies = list(thinking_bodies)
        self._text = text

    async def complete(
        self, messages, tools, system_prompt, model, *,
        on_text_chunk=None,
        on_thinking_start=None,
        on_thinking_end=None,
        cache_system_prefix="", cache_last_human_turn=False,
        thinking=False, thinking_budget=8000, thinking_effort="high",
    ):
        for body in self._bodies:
            if on_thinking_start is not None:
                on_thinking_start()
            if on_thinking_end is not None:
                on_thinking_end(body)
        if on_text_chunk is not None and self._text:
            on_text_chunk(self._text)
        return (self._text, [], TokenUsage(input_tokens=4, output_tokens=2))


@pytest.mark.asyncio
async def test_session_chat_emits_thinking_start_done_events_not_partial_text(tmp_path):
    """Thinking blocks emit dedicated IPC events; nothing leaks into partial_text.

    Regression for v2.0.9 spec: "correctly capture thinking" + "don't show
    truncated thinking inline".
    """
    agent = Agent(provider=ThinkingProvider(thinking_bodies=["step 1\nstep 2"]))
    session = make_session(tmp_path, agent)
    session._load_session_capabilities = lambda: None
    session._ipc = FileIPC(session.system_dir)

    await session.chat("hi")

    runtime_events = read_jsonl(session.system_dir / "events.jsonl")
    types = [e["type"] for e in runtime_events]
    assert "thinking_start" in types
    assert "thinking_done" in types
    # partial_text only carries the assistant text — no "step 1" / "step 2" fragments.
    partials = [e for e in runtime_events if e["type"] == "partial_text"]
    for pt in partials:
        assert "step 1" not in pt["content"]
        assert "step 2" not in pt["content"]

    done = next(e for e in runtime_events if e["type"] == "thinking_done")
    assert done["text"] == "step 1\nstep 2"
    assert isinstance(done["block_id"], str) and done["block_id"].startswith("th:")
    assert isinstance(done["duration_ms"], int) and done["duration_ms"] >= 0

    start = next(e for e in runtime_events if e["type"] == "thinking_start")
    assert start["block_id"] == done["block_id"]


@pytest.mark.asyncio
async def test_session_chat_marks_turn_with_has_streaming_thinking(tmp_path):
    """When thinking fires, the persisted turn records has_streaming_thinking
    so live SSE replay can suppress the inline-thinking emit (dedup).
    """
    agent = Agent(provider=ThinkingProvider(thinking_bodies=["reasoning"]))
    session = make_session(tmp_path, agent)
    session._load_session_capabilities = lambda: None
    session._ipc = FileIPC(session.system_dir)

    await session.chat("hi")

    context_events = read_jsonl(session.system_dir / "context.jsonl")
    turn = next(e for e in context_events if e["type"] == "turn")
    assert turn.get("has_streaming_thinking") is True


# ── v2.0.19 tool_done carries truncated result ───────────────────────────────


@pytest.mark.asyncio
async def test_tool_done_event_carries_small_result_without_truncation_flag(tmp_path):
    """v2.0.19: tool_done ships the tool's result text (capped at 8 KB) so
    the UI's inline <details> body can render it live. Results under the
    cap must NOT carry a result_truncated flag."""
    provider = MockProvider([
        ("planning", [ToolCall(id="tc-1", name="echo_tool", input={"text": "ping"})], TokenUsage()),
        ("done", [], TokenUsage()),
    ])

    @tool(description="Echo a value")
    async def echo_tool(text: str) -> str:
        return f"echo:{text}"

    agent = Agent(provider=provider, tools=[echo_tool])
    session = make_session(tmp_path, agent)
    session._load_session_capabilities = lambda: None
    session._ipc = FileIPC(session.system_dir)

    await session.chat("hi")

    runtime_events = read_jsonl(session.system_dir / "events.jsonl")
    tool_done = next(e for e in runtime_events if e["type"] == "tool_done")
    assert tool_done["result"] == "echo:ping"
    assert tool_done["result_len"] == len("echo:ping")
    # Flag is omitted entirely for under-cap results.
    assert "result_truncated" not in tool_done


@pytest.mark.asyncio
async def test_tool_done_event_caps_huge_result_and_sets_truncation_flag(tmp_path):
    """Results over the 8 KB cap are trimmed; result_len stays full so the
    UI can surface the original size, and result_truncated=true tells the
    frontend to append a "…[truncated]" hint."""
    huge = "A" * 20_000
    provider = MockProvider([
        ("planning", [ToolCall(id="tc-1", name="loud_tool", input={})], TokenUsage()),
        ("done", [], TokenUsage()),
    ])

    @tool(description="Emit a huge blob")
    async def loud_tool() -> str:
        return huge

    agent = Agent(provider=provider, tools=[loud_tool])
    session = make_session(tmp_path, agent)
    session._load_session_capabilities = lambda: None
    session._ipc = FileIPC(session.system_dir)

    await session.chat("hi")

    runtime_events = read_jsonl(session.system_dir / "events.jsonl")
    tool_done = next(e for e in runtime_events if e["type"] == "tool_done")
    assert tool_done["result_len"] == 20_000
    assert tool_done["result_truncated"] is True
    assert len(tool_done["result"]) == 8000
    assert tool_done["result"] == "A" * 8000


def test_context_event_to_display_suppresses_inline_thinking_for_live_when_streamed(tmp_path):
    """For live SSE, has_streaming_thinking on a turn must prevent re-emitting
    the inline thinking content (the live events already rendered the cell).
    History replay (for_history=True) still emits so the transcript is complete.
    """
    turn = {
        "type": "turn",
        "ts": "2026-04-15T00:00:00",
        "triggered_by": "user",
        "has_streaming_thinking": True,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "should-not-duplicate"},
                    {"type": "text", "text": "final"},
                ],
            }
        ],
    }
    sse = _context_event_to_display(turn, for_history=False)
    assert not any(e["type"] == "thinking" for e in sse), "live SSE must not re-emit thinking when already streamed"
    hist = _context_event_to_display(turn, for_history=True)
    assert any(e["type"] == "thinking" and e["content"] == "should-not-duplicate" for e in hist)
