"""Tests for v2.0.20 / v2.0.21 agent-output duration instrumentation.

Pins the contract between:

* ``Session._make_text_chunk_callback`` — stamps
  ``self._text_output_started_at`` on the first non-empty chunk of each
  LLM call and emits ``agent_output_start`` to events.jsonl.
* ``Session._make_llm_call_end_callback`` — on call end, if a start
  stamp was set, emits ``agent_output_done`` with ``duration_ms`` AND
  appends the duration to ``self._current_turn_agent_durations``.
* ``_do_chat`` / ``_do_tick`` — reset ``_current_turn_agent_durations``
  at the start of each run, drain it onto ``turn["agent_output_durations"]``
  via the turn writer, and clear ``_text_output_started_at`` in the
  ``finally`` block so a cancelled run never leaks stale timing into the
  next run's first LLM call.

The last point is the v2.0.21 PR-review regression fix: ``_do_chat`` had
the reset, ``_do_tick`` was missing it. ``test_tick_cancel_clears_text_output_started_at``
is the pin against that specific bug.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from butterfly.core.agent import Agent
from butterfly.core.provider import Provider
from butterfly.core.types import TokenUsage
from butterfly.session_engine.session import Session


class StreamingProvider(Provider):
    """Streams each character of ``text`` through ``on_text_chunk`` then
    returns, simulating a provider that produces text output."""

    def __init__(self, text: str = "", *, tool_calls: list | None = None,
                 pre_sleep: float = 0.0, chunk_sleep: float = 0.0):
        self._text = text
        self._tool_calls = tool_calls or []
        self._pre_sleep = pre_sleep
        self._chunk_sleep = chunk_sleep
        self.calls = 0

    async def complete(self, messages, tools, system_prompt, model, *,
                       on_text_chunk=None, cache_system_prefix="",
                       cache_last_human_turn=False, thinking: bool = False,
                       thinking_budget: int = 8000, thinking_effort: str = "high",
                       on_thinking_start=None, on_thinking_end=None):
        self.calls += 1
        if self._pre_sleep:
            await asyncio.sleep(self._pre_sleep)
        if on_text_chunk and self._text:
            for ch in self._text:
                on_text_chunk(ch)
                if self._chunk_sleep:
                    await asyncio.sleep(self._chunk_sleep)
        return (self._text, list(self._tool_calls), TokenUsage(input_tokens=1, output_tokens=1))


def make_session(tmp_path: Path, agent: Agent, *, session_id: str = "demo") -> Session:
    system_base = tmp_path / "_sessions"
    session = Session(agent=agent, session_id=session_id,
                      base_dir=tmp_path, system_base=system_base)
    (session.core_dir / "system.md").write_text("", encoding="utf-8")
    (session.core_dir / "task.md").write_text("", encoding="utf-8")
    (session.core_dir / "env.md").write_text("", encoding="utf-8")
    session._load_session_capabilities = lambda: None  # type: ignore[method-assign]
    return session


def read_events(session: Session) -> list[dict]:
    path = session.system_dir / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def read_context(session: Session) -> list[dict]:
    path = session.system_dir / "context.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── agent_output_start / agent_output_done SSE contract ──────────────────


class TestAgentOutputLifecycleEvents:
    def test_agent_output_start_emitted_on_first_chunk(self, tmp_path):
        """The first non-empty chunk of an LLM call must emit exactly one
        ``agent_output_start`` event — subsequent chunks of the same call
        MUST NOT re-emit it. This is what lets the frontend open the
        "Typing…" cell instantly while deferring text render until later."""
        agent = Agent(provider=StreamingProvider("hello world"))
        session = make_session(tmp_path, agent)
        asyncio.run(session.chat("hi"))
        starts = [e for e in read_events(session) if e["type"] == "agent_output_start"]
        assert len(starts) == 1

    def test_agent_output_done_emitted_with_duration(self, tmp_path):
        """When an LLM call produces text, ``on_llm_call_end`` must emit
        an ``agent_output_done`` event carrying a positive ``duration_ms``."""
        agent = Agent(provider=StreamingProvider("some output"))
        session = make_session(tmp_path, agent)
        asyncio.run(session.chat("hi"))
        done = [e for e in read_events(session) if e["type"] == "agent_output_done"]
        assert len(done) == 1
        assert done[0]["duration_ms"] >= 0
        assert "iteration" in done[0]

    def test_agent_output_done_silent_when_no_text(self, tmp_path):
        """An LLM call that produced no text (tool-only / empty response)
        MUST NOT emit ``agent_output_done`` — the frontend would otherwise
        render an empty "Agent 0.0s" ghost cell on reload."""
        # Provider returns empty text AND no tool calls — single iteration,
        # no text output.
        agent = Agent(provider=StreamingProvider(""))
        session = make_session(tmp_path, agent)
        asyncio.run(session.chat("hi"))
        done = [e for e in read_events(session) if e["type"] == "agent_output_done"]
        starts = [e for e in read_events(session) if e["type"] == "agent_output_start"]
        assert len(done) == 0
        assert len(starts) == 0

    def test_text_output_started_at_is_reset_after_llm_call_end(self, tmp_path):
        """After ``on_llm_call_end`` fires, ``_text_output_started_at``
        MUST be ``None`` so the NEXT LLM call's first chunk restamps a
        fresh timestamp. Otherwise a multi-call turn would measure only
        the first call's duration (or worse, the delta between call
        boundaries) for every call."""
        agent = Agent(provider=StreamingProvider("out"))
        session = make_session(tmp_path, agent)
        asyncio.run(session.chat("hi"))
        assert session._text_output_started_at is None


# ── Per-turn agent_output_durations persistence ──────────────────────────


class TestTurnAgentOutputDurations:
    def test_chat_turn_persists_agent_output_durations(self, tmp_path):
        """A chat run that produced text must persist the measured output
        duration(s) under ``turn["agent_output_durations"]``. The list has
        one entry per LLM call that produced text, in iteration order."""
        agent = Agent(provider=StreamingProvider("hello"))
        session = make_session(tmp_path, agent)
        asyncio.run(session.chat("hi"))
        turns = [e for e in read_context(session) if e["type"] == "turn"]
        assert len(turns) == 1
        assert "agent_output_durations" in turns[0]
        assert len(turns[0]["agent_output_durations"]) == 1
        assert turns[0]["agent_output_durations"][0] >= 0

    def test_current_turn_agent_durations_reset_at_run_start(self, tmp_path):
        """Two sequential chat runs on the same session must each see a
        fresh, independent duration list — no bleed from the prior run.
        The list is reset at the START of each ``_do_chat`` / ``_do_tick``
        (the comment at session.py:704-707 is explicit about this)."""
        agent = Agent(provider=StreamingProvider("one"))
        session = make_session(tmp_path, agent)
        asyncio.run(session.chat("first"))
        asyncio.run(session.chat("second"))
        turns = [e for e in read_context(session) if e["type"] == "turn"]
        assert len(turns) == 2
        # Each turn has its own list of length 1 — no accumulation.
        assert len(turns[0]["agent_output_durations"]) == 1
        assert len(turns[1]["agent_output_durations"]) == 1

    def test_turn_without_text_output_omits_durations_field(self, tmp_path):
        """When the LLM produced no text (tool-only iteration followed by
        an empty final call), the turn MUST NOT carry an empty
        ``agent_output_durations: []`` — the key should simply be absent,
        matching the pre-v2.0.20 schema that history replay falls back to."""
        agent = Agent(provider=StreamingProvider(""))
        session = make_session(tmp_path, agent)
        asyncio.run(session.chat("hi"))
        turns = [e for e in read_context(session) if e["type"] == "turn"]
        assert len(turns) == 1
        assert "agent_output_durations" not in turns[0]


# ── Cancellation-state hygiene (the v2.0.21 bug I flagged) ───────────────


class TestCancellationResetsStreamingState:
    """The bug: ``_do_chat``'s finally clears ``_text_output_started_at``;
    ``_do_tick``'s finally was missing the same reset. A tick cancelled
    between ``on_chunk`` (first-chunk stamp) and ``on_llm_call_end``
    (consumes + clears) leaves a stale monotonic timestamp on the Session
    instance — the NEXT ``_do_chat`` / ``_do_tick`` then measures its
    first call's duration as ``monotonic_now - stale_ts``, which is the
    wall-clock gap between runs plus the real call duration.

    Fix landed in 7f2c961: both finally blocks now perform the reset.
    """

    def test_chat_finally_clears_text_output_started_at_on_cancel(self, tmp_path):
        """Pre-existing contract: ``_do_chat`` clears the streaming-start
        timestamp in its finally block. Cancel during the provider's
        ``await`` (before any text chunk reaches us) — start stays None
        naturally. Cancel AFTER chunk one emits — finally must reset."""
        # 50ms pre-sleep then streams "hello" slowly so we can cancel
        # after the first chunk has been emitted.
        provider = StreamingProvider("hello", pre_sleep=0.01, chunk_sleep=0.01)
        agent = Agent(provider=provider)
        session = make_session(tmp_path, agent)

        async def run():
            t = asyncio.create_task(session.chat("hi"))
            # Sleep long enough for pre_sleep + 1-2 chunks to land.
            await asyncio.sleep(0.05)
            t.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t
        asyncio.run(run())
        assert session._text_output_started_at is None

    def test_tick_finally_clears_text_output_started_at_on_cancel(self, tmp_path):
        """Regression pin for the PR-review bug: a tick cancelled mid-stream
        must leave ``_text_output_started_at`` as ``None`` — otherwise the
        NEXT ``_do_chat`` / ``_do_tick`` would compute an inflated
        ``agent_output_done.duration_ms`` for its first LLM call.

        Without the v2.0.21 fix in _do_tick's finally, this assertion fails:
        the timestamp leaks and the session holds a monotonic value pointing
        at the cancelled tick's first chunk moment."""
        from butterfly.session_engine.task_cards import TaskCard, save_card
        from datetime import datetime, timedelta

        provider = StreamingProvider("hello", pre_sleep=0.01, chunk_sleep=0.01)
        agent = Agent(provider=provider, task_prompt="do {task}")
        session = make_session(tmp_path, agent)
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        save_card(
            session.tasks_dir,
            TaskCard(name="t", description="test", interval=600, start_at=past),
        )

        async def run():
            t = asyncio.create_task(session.tick())
            await asyncio.sleep(0.05)  # let pre_sleep + first chunk(s) land
            t.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t
        asyncio.run(run())
        assert session._text_output_started_at is None, (
            "_do_tick's finally block must clear _text_output_started_at "
            "(v2.0.21 PR-review regression): a stranded timestamp from a "
            "cancelled tick would pollute the next run's first LLM-call "
            "duration measurement."
        )


# ── tool_use_id on tool_done — required for history-replay duration pairing


class TestToolDoneCarriesToolUseId:
    def test_tool_done_event_carries_tool_use_id(self, tmp_path):
        """``on_tool_done`` must include ``tool_use_id`` on the persisted
        event; ``FileIPC._scan_tool_durations`` keys the duration map off
        it so history replay can match a replayed ``tool`` block back to
        its wall-clock duration. Missing this field silently strips every
        "✓ bash 2.4s …" pill on reload. Paired call/done also populates
        ``duration_ms`` via the ``_tool_started`` monotonic map."""
        import time as _time

        agent = Agent(provider=StreamingProvider(""))
        session = make_session(tmp_path, agent)
        call_cb, _ = session._make_tool_call_callback()
        done_cb = session._make_tool_done_callback()
        # Real Agent loop always pairs call → done by tool_use_id.
        call_cb("bash", {"command": "echo hi"}, "use_xyz")
        _time.sleep(0.005)
        done_cb("bash", {"command": "echo hi"}, "hi\n", "use_xyz")
        events = [e for e in read_events(session) if e["type"] == "tool_done"]
        assert len(events) == 1
        assert events[0]["tool_use_id"] == "use_xyz"
        assert events[0]["name"] == "bash"
        assert "duration_ms" in events[0]
        assert events[0]["duration_ms"] >= 0
