"""Tests for _make_text_chunk_callback flush behaviour.

Verifies that:
1. The callback has a .flush() attribute.
2. Chunks below the threshold are NOT emitted until flush().
3. flush() emits the remaining buffer as a partial_text event.
4. chat() and tick() call flush() so no text is lost.
"""
import json
import asyncio

import pytest

from nutshell.core.provider import Provider
from nutshell.core.agent import Agent
from nutshell.session_engine.session import Session


class MockProvider(Provider):
    def __init__(self, responses):
        self._responses = iter(responses)

    async def complete(self, messages, tools, system_prompt, model, *,
                       on_text_chunk=None, cache_system_prefix="",
                       cache_last_human_turn=False, thinking: bool = False, thinking_budget: int = 8000, thinking_effort: str = "high"):
        from nutshell.core.types import TokenUsage
        r = next(self._responses)
        text = r[0]
        # Simulate streaming: send text in small chunks to on_text_chunk
        if on_text_chunk and text:
            for char in text:
                on_text_chunk(char)
        return (text, r[1], r[2] if len(r) > 2 else TokenUsage())


def make_session(tmp_path, agent, session_id="demo"):
    system_base = tmp_path / "_sessions"
    session = Session(agent=agent, session_id=session_id,
                      base_dir=tmp_path, system_base=system_base)
    (session.core_dir / "system.md").write_text(
        agent.system_prompt or "", encoding="utf-8")
    (session.core_dir / "heartbeat.md").write_text(
        getattr(agent, "heartbeat_prompt", "") or "", encoding="utf-8")
    (session.core_dir / "session.md").write_text(
        getattr(agent, "session_context_template", "") or "", encoding="utf-8")
    return session


def read_events(session):
    events_path = session.system_dir / "events.jsonl"
    if not events_path.exists():
        return []
    lines = events_path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(l) for l in lines]


# ── Unit tests for the callback itself ──────────────────────────

class TestMakeTextChunkCallback:
    def test_flush_attribute_exists(self, tmp_path):
        """Returned callback must have a callable .flush() attribute."""
        agent = Agent(provider=MockProvider([]), model="test")
        session = make_session(tmp_path, agent)
        cb = session._make_text_chunk_callback()
        assert hasattr(cb, "flush")
        assert callable(cb.flush)

    def test_small_chunks_buffered(self, tmp_path):
        """Chunks smaller than threshold should NOT produce events yet."""
        agent = Agent(provider=MockProvider([]), model="test")
        session = make_session(tmp_path, agent)
        cb = session._make_text_chunk_callback()
        cb("hello")  # 5 chars — well below 150 threshold
        events = [e for e in read_events(session) if e["type"] == "partial_text"]
        assert len(events) == 0

    def test_flush_emits_remaining(self, tmp_path):
        """flush() should emit whatever is in the buffer."""
        agent = Agent(provider=MockProvider([]), model="test")
        session = make_session(tmp_path, agent)
        cb = session._make_text_chunk_callback()
        cb("hello world")
        cb.flush()
        events = [e for e in read_events(session) if e["type"] == "partial_text"]
        assert len(events) == 1
        assert events[0]["content"] == "hello world"

    def test_flush_idempotent(self, tmp_path):
        """Calling flush() twice should not emit duplicate events."""
        agent = Agent(provider=MockProvider([]), model="test")
        session = make_session(tmp_path, agent)
        cb = session._make_text_chunk_callback()
        cb("data")
        cb.flush()
        cb.flush()  # second call — buffer is empty
        events = [e for e in read_events(session) if e["type"] == "partial_text"]
        assert len(events) == 1

    def test_threshold_plus_remainder(self, tmp_path):
        """Text exceeding threshold should auto-flush, then flush() gets the rest."""
        agent = Agent(provider=MockProvider([]), model="test")
        session = make_session(tmp_path, agent)
        cb = session._make_text_chunk_callback()
        # Send 160 chars (above 150 threshold) then 30 more
        cb("A" * 160)
        cb("B" * 30)
        cb.flush()
        events = [e for e in read_events(session) if e["type"] == "partial_text"]
        # First auto-flush at 160 chars, then flush() for the 30 remainder
        assert len(events) == 2
        assert events[0]["content"] == "A" * 160
        assert events[1]["content"] == "B" * 30


# ── Integration tests: chat() and tick() flush ──────────────────

class TestChatFlush:
    def test_chat_flushes_remaining_text(self, tmp_path):
        """chat() must flush remaining buffered text after agent.run()."""
        # Response is 50 chars — below 150 threshold, so only flush() will emit it
        short_text = "x" * 50
        provider = MockProvider([(short_text, [])])
        agent = Agent(provider=provider, model="test")
        session = make_session(tmp_path, agent)
        asyncio.run(session.chat("hi"))
        events = [e for e in read_events(session) if e["type"] == "partial_text"]
        # The text must appear (via flush), not be lost
        assert len(events) >= 1
        total_text = "".join(e["content"] for e in events)
        assert total_text == short_text


class TestTickFlush:
    def test_tick_flushes_remaining_text(self, tmp_path):
        """tick() must flush remaining buffered text after agent.run()."""
        short_text = "y" * 50
        provider = MockProvider([(short_text, [])])
        agent = Agent(provider=provider, model="test",
                      heartbeat_prompt="do stuff\n{tasks}")
        session = make_session(tmp_path, agent)
        # Write tasks so tick() actually runs
        session.tasks_path.write_text("- test task\n", encoding="utf-8")
        asyncio.run(session.tick())
        events = [e for e in read_events(session) if e["type"] == "partial_text"]
        assert len(events) >= 1
        total_text = "".join(e["content"] for e in events)
        assert total_text == short_text
