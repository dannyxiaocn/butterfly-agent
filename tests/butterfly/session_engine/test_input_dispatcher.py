"""Tests for the v2.0.12 input dispatcher.

Covers the four user-visible cases from the spec:

* Wait merging: two ``mode=wait`` chats arriving while a third is in flight
  collapse into a single user turn (verified by recording the user inputs
  the agent actually sees).
* Interrupt + uncommitted merge: a ``mode=interrupt`` chat that arrives
  before the LLM has emitted any response cancels the in-flight run and
  the cancelled content is folded into the new user turn.
* Interrupt + committed: a ``mode=interrupt`` chat that arrives after the
  LLM has produced at least one assistant turn does NOT merge; the
  cancelled prefix is persisted as ``interrupted: True`` and the new
  chat runs with a fresh user turn.
* Default-mode routing for sources: the daemon enqueues background-tool
  notifications (``source=panel``) as interrupt and task wakeups as wait.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from butterfly.core.agent import Agent
from butterfly.core.provider import Provider
from butterfly.core.types import Message, TokenUsage, ToolCall
from butterfly.session_engine.pending_inputs import (
    ChatItem,
    TaskItem,
    default_mode_for_source,
    merge_chat_content,
)
from butterfly.session_engine.session import Session


# ── Test helpers ─────────────────────────────────────────────────────────────


class RecordingProvider(Provider):
    """Returns a sequence of (text, tool_calls) responses, recording inputs."""

    def __init__(self, responses, *, observed_user_messages: list[str] | None = None):
        self._responses = list(responses)
        self._calls = 0
        self.observed_user_messages = observed_user_messages if observed_user_messages is not None else []

    async def complete(
        self,
        messages,
        tools,
        system_prompt,
        model,
        *,
        on_text_chunk=None,
        cache_system_prefix="",
        cache_last_human_turn=False,
        thinking=False,
        thinking_budget=8000,
        thinking_effort="high",
        on_thinking_start=None,
        on_thinking_end=None,
    ):
        # Record the most recent user message in the messages list (the
        # tail user is what this iteration is "responding to")
        for m in reversed(messages):
            if m.role == "user":
                self.observed_user_messages.append(
                    m.content if isinstance(m.content, str) else str(m.content)
                )
                break
        idx = self._calls
        self._calls += 1
        if idx >= len(self._responses):
            return ("", [], TokenUsage())
        text, tool_calls = self._responses[idx]
        return (text, tool_calls, TokenUsage(input_tokens=1, output_tokens=1))


class SlowProvider(Provider):
    """Sleeps before returning so a parallel chat() can interrupt it."""

    def __init__(self, *, delay: float, response_after: tuple[str, list] = ("done", []), observed_user_messages=None):
        self.delay = delay
        self.response_after = response_after
        self.observed_user_messages = observed_user_messages if observed_user_messages is not None else []
        self._call_count = 0

    async def complete(
        self,
        messages,
        tools,
        system_prompt,
        model,
        *,
        on_text_chunk=None,
        cache_system_prefix="",
        cache_last_human_turn=False,
        thinking=False,
        thinking_budget=8000,
        thinking_effort="high",
        on_thinking_start=None,
        on_thinking_end=None,
    ):
        for m in reversed(messages):
            if m.role == "user":
                self.observed_user_messages.append(
                    m.content if isinstance(m.content, str) else str(m.content)
                )
                break
        self._call_count += 1
        await asyncio.sleep(self.delay)
        return (self.response_after[0], self.response_after[1], TokenUsage(input_tokens=1, output_tokens=1))


def make_session(tmp_path, agent, session_id="dispatcher"):
    from butterfly.runtime.ipc import FileIPC

    system_base = tmp_path / "_sessions"
    session = Session(
        agent=agent,
        session_id=session_id,
        base_dir=tmp_path,
        system_base=system_base,
    )
    (session.core_dir / "system.md").write_text("", encoding="utf-8")
    (session.core_dir / "task.md").write_text("", encoding="utf-8")
    (session.core_dir / "env.md").write_text("", encoding="utf-8")
    session._ipc = FileIPC(session.system_dir)
    # Skip capability reload — we want the provider as-given
    session._load_session_capabilities = lambda: None  # type: ignore[method-assign]
    return session


def read_jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── Pure unit tests on the queue items ───────────────────────────────────────


def test_default_mode_for_source():
    assert default_mode_for_source("user") == "interrupt"
    assert default_mode_for_source("panel") == "interrupt"
    assert default_mode_for_source("task") == "wait"
    assert default_mode_for_source("anything-else") == "interrupt"


def test_merge_chat_content_concatenates():
    assert merge_chat_content("a", "b") == "a\n\nb"
    assert merge_chat_content("", "b") == "b"
    assert merge_chat_content("a", "") == "a"


def test_chat_item_merge_after_appends_content_and_futures():
    loop = asyncio.new_event_loop()
    try:
        a_fut = loop.create_future()
        b_fut = loop.create_future()
        a = ChatItem(content="hello", mode="wait", user_input_ids=["a"], futures=[a_fut])
        b = ChatItem(content="world", mode="wait", user_input_ids=["b"], futures=[b_fut])
        a.merge_after(b)
        assert a.content == "hello\n\nworld"
        assert a.user_input_ids == ["a", "b"]
        assert a.futures == [a_fut, b_fut]
        assert a.latest_user_input_id == "b"
    finally:
        loop.close()


def test_chat_item_merge_before_prepends_content_and_keeps_latest_id():
    loop = asyncio.new_event_loop()
    try:
        cancelled_fut = loop.create_future()
        new_fut = loop.create_future()
        cancelled = ChatItem(content="A", mode="interrupt", user_input_ids=["a"], futures=[cancelled_fut])
        new = ChatItem(content="B", mode="interrupt", user_input_ids=["b"], futures=[new_fut])
        new.merge_before(cancelled)
        assert new.content == "A\n\nB"
        assert new.user_input_ids == ["a", "b"]
        # latest is still ``new``'s id
        assert new.latest_user_input_id == "b"
        # Both futures now ride on this item
        assert new.futures == [cancelled_fut, new_fut]
    finally:
        loop.close()


def test_chat_item_rejects_invalid_mode():
    with pytest.raises(ValueError):
        ChatItem(content="x", mode="bogus")


# ── End-to-end dispatcher behaviour ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_mode_merges_consecutive_chats(tmp_path):
    """Two wait-mode chats sent while a first chat is in flight collapse
    into a single agent run on top of the first."""
    observed: list[str] = []
    provider = SlowProvider(delay=0.15, response_after=("ok", []), observed_user_messages=observed)
    agent = Agent(provider=provider)
    session = make_session(tmp_path, agent)

    # Kick off the first run; it sleeps 150ms inside complete().
    first_task = asyncio.create_task(session.chat("first", mode="interrupt"))
    await asyncio.sleep(0.02)
    # Two wait-mode chats while the first is in flight — should merge.
    second_task = asyncio.create_task(session.chat("second", mode="wait"))
    await asyncio.sleep(0.01)
    third_task = asyncio.create_task(session.chat("third", mode="wait"))

    results = await asyncio.gather(first_task, second_task, third_task)
    # First run sees its own message; the merged run sees "second\n\nthird"
    assert observed == ["first", "second\n\nthird"]
    # Second + third resolve to the SAME AgentResult (merged into one run)
    assert results[1] is results[2]
    # And that result is distinct from the first
    assert results[0] is not results[1]


@pytest.mark.asyncio
async def test_interrupt_uncommitted_merges_cancelled_with_new(tmp_path):
    """A mode=interrupt chat that arrives before the in-flight run has
    committed any assistant turn cancels the run and merges its content
    with the new chat into a single user turn."""
    observed: list[str] = []
    # Long delay → first complete() never returns before cancel.
    provider = SlowProvider(delay=5.0, observed_user_messages=observed)
    agent = Agent(provider=provider)
    session = make_session(tmp_path, agent)

    first = asyncio.create_task(session.chat("first", mode="interrupt"))
    await asyncio.sleep(0.05)
    # Switch the provider so the second run, which uses the merged input,
    # returns quickly.
    fast_provider = RecordingProvider([("merged-ok", [])], observed_user_messages=observed)
    agent._provider = fast_provider
    second = asyncio.create_task(session.chat("second", mode="interrupt"))

    # Both futures should resolve via the merged run
    res1, res2 = await asyncio.gather(first, second)
    assert res1 is res2
    # The fast provider observed exactly one call, with merged content
    assert fast_provider.observed_user_messages[-1] == "first\n\nsecond"


@pytest.mark.asyncio
async def test_interrupt_committed_does_not_merge(tmp_path):
    """A mode=interrupt chat that arrives AFTER the in-flight run has
    committed at least one assistant turn does NOT merge the inputs;
    the cancelled prefix lands on disk as ``interrupted: True`` and the
    new chat runs with a fresh user message."""
    observed: list[str] = []

    class CommitThenStallProvider(Provider):
        """First call returns a tool-using assistant turn (commits to history),
        then on the second iteration sleeps long enough to be cancelled."""

        def __init__(self):
            self.calls = 0

        async def complete(
            self,
            messages,
            tools,
            system_prompt,
            model,
            *,
            on_text_chunk=None,
            cache_system_prefix="",
            cache_last_human_turn=False,
            thinking=False,
            thinking_budget=8000,
            thinking_effort="high",
            on_thinking_start=None,
            on_thinking_end=None,
        ):
            for m in reversed(messages):
                if m.role == "user":
                    observed.append(m.content if isinstance(m.content, str) else str(m.content))
                    break
            self.calls += 1
            if self.calls == 1:
                # Commit a tool_use assistant turn so the agent loop appends
                # to history before re-entering complete().
                return ("", [ToolCall(id="tc1", name="noop_tool", input={})], TokenUsage(input_tokens=1, output_tokens=1))
            await asyncio.sleep(5.0)
            return ("never", [], TokenUsage())

    from butterfly.core.tool import tool

    @tool(description="no-op")
    async def noop_tool() -> str:  # noqa: D401
        return "ok"

    provider = CommitThenStallProvider()
    agent = Agent(provider=provider, tools=[noop_tool])
    session = make_session(tmp_path, agent)

    first = asyncio.create_task(session.chat("first", mode="interrupt"))
    # Wait long enough for first iteration to commit + second iteration to be in flight
    await asyncio.sleep(0.15)
    # Swap to fast provider for the new run
    fast = RecordingProvider([("post-interrupt", [])], observed_user_messages=observed)
    agent._provider = fast
    second = asyncio.create_task(session.chat("second", mode="interrupt"))

    # First future is rejected with CancelledError (committed prefix saved)
    with pytest.raises(asyncio.CancelledError):
        await first
    res2 = await second
    assert res2.content == "post-interrupt"

    # Disk evidence: an interrupted-flagged turn for "first", a normal turn for "second"
    ctx_path = session.system_dir / "context.jsonl"
    events = read_jsonl(ctx_path)
    turn_types = [
        (ev.get("interrupted", False), ev["messages"][0]["content"])
        for ev in events
        if ev.get("type") == "turn"
    ]
    # First entry: interrupted=True for "first"
    assert turn_types[0] == (True, "first")
    # Second entry: not interrupted, for "second"
    assert turn_types[1] == (False, "second")


@pytest.mark.asyncio
async def test_explicit_interrupt_drops_inbox_and_cancels_run(tmp_path):
    """Bare interrupt (send_interrupt) cancels the in-flight run AND drops
    every queued item with CancelledError."""
    observed: list[str] = []
    provider = SlowProvider(delay=5.0, observed_user_messages=observed)
    agent = Agent(provider=provider)
    session = make_session(tmp_path, agent)

    in_flight = asyncio.create_task(session.chat("first", mode="interrupt"))
    await asyncio.sleep(0.05)
    queued = asyncio.create_task(session.chat("queued", mode="wait"))
    await asyncio.sleep(0.02)

    await session._handle_explicit_interrupt(0)

    with pytest.raises(asyncio.CancelledError):
        await in_flight
    with pytest.raises(asyncio.CancelledError):
        await queued

    # An interrupted event recorded with cancelled_run=True
    events_path = session.system_dir / "events.jsonl"
    events = read_jsonl(events_path)
    interrupted = [e for e in events if e.get("type") == "interrupted"]
    assert interrupted
    assert interrupted[-1]["cancelled_run"] is True


@pytest.mark.asyncio
async def test_send_message_records_mode_in_context(tmp_path):
    """BridgeSession.send_message writes the mode field so the daemon knows
    which queue semantics to apply."""
    from butterfly.runtime.bridge import BridgeSession

    system_dir = tmp_path / "_sessions" / "x"
    system_dir.mkdir(parents=True)
    bridge = BridgeSession(system_dir)
    bridge.send_message("hi", mode="wait")
    bridge.send_message("yo")  # default → interrupt

    events = read_jsonl(system_dir / "context.jsonl")
    assert events[0]["mode"] == "wait"
    assert events[1]["mode"] == "interrupt"


@pytest.mark.asyncio
async def test_send_message_rejects_invalid_mode(tmp_path):
    from butterfly.runtime.bridge import BridgeSession

    system_dir = tmp_path / "_sessions" / "x"
    system_dir.mkdir(parents=True)
    bridge = BridgeSession(system_dir)
    with pytest.raises(ValueError):
        bridge.send_message("hi", mode="bogus")


@pytest.mark.asyncio
async def test_daemon_loop_routes_panel_user_input_through_queue(tmp_path):
    """Verifies the daemon polls a user_input event with mode=interrupt
    and source=panel and runs it via the dispatcher."""
    from butterfly.runtime.ipc import FileIPC

    observed: list[str] = []
    provider = RecordingProvider([("ack", [])], observed_user_messages=observed)
    agent = Agent(provider=provider)
    session = make_session(tmp_path, agent, session_id="daemon-route")
    ipc = FileIPC(session.system_dir)

    stop_event = asyncio.Event()

    # Patch asyncio.sleep to a faster cadence so the test doesn't take 1s+
    real_sleep = asyncio.sleep

    async def _fast_sleep(seconds):
        await real_sleep(min(seconds, 0.01))

    import butterfly.session_engine.session as session_mod
    session_mod.asyncio.sleep = _fast_sleep  # type: ignore[assignment]

    try:
        daemon_task = asyncio.create_task(
            session.run_daemon_loop(ipc, stop_event=stop_event)
        )
        # Give the daemon one tick before writing the input
        await real_sleep(0.05)
        ipc.append_context({
            "type": "user_input",
            "content": "bg-tool finished",
            "id": "bg-1",
            "caller": "system",
            "source": "panel",
            "mode": "interrupt",
        })
        # Wait for the consumer to pick up + run
        for _ in range(50):
            if observed:
                break
            await real_sleep(0.02)
        stop_event.set()
        await daemon_task
    finally:
        session_mod.asyncio.sleep = real_sleep  # type: ignore[assignment]

    # The dispatcher ran the panel input → provider observed it
    assert observed == ["bg-tool finished"]
    # Turn was written with the originating msg_id
    turns = [
        e for e in read_jsonl(session.system_dir / "context.jsonl")
        if e.get("type") == "turn"
    ]
    assert turns
    assert turns[0]["user_input_id"] == "bg-1"


@pytest.mark.asyncio
async def test_merged_user_input_ids_recorded_on_turn(tmp_path):
    """When a wait-merged turn fires, the resulting turn event records
    every contributing msg_id in ``merged_user_input_ids``."""
    observed: list[str] = []
    provider = SlowProvider(delay=0.10, observed_user_messages=observed)
    agent = Agent(provider=provider)
    session = make_session(tmp_path, agent)

    first = asyncio.create_task(
        session.chat("first", mode="interrupt", user_input_id="id-1")
    )
    await asyncio.sleep(0.02)
    second = asyncio.create_task(
        session.chat("second", mode="wait", user_input_id="id-2")
    )
    await asyncio.sleep(0.005)
    third = asyncio.create_task(
        session.chat("third", mode="wait", user_input_id="id-3")
    )

    await asyncio.gather(first, second, third)

    turns = [e for e in read_jsonl(session.system_dir / "context.jsonl") if e.get("type") == "turn"]
    # Two turns: the first one (id-1 only), then the wait-merged one (id-2 + id-3)
    assert turns[0]["user_input_id"] == "id-1"
    assert "merged_user_input_ids" not in turns[0]
    assert turns[1]["user_input_id"] == "id-3"
    assert turns[1]["merged_user_input_ids"] == ["id-2", "id-3"]
