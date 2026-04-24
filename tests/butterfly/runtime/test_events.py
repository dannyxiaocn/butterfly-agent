"""Tests for butterfly.runtime.events — Phase 1 IO foundation.

Covers the primitives API defined in DESIGN.md §5.2 and the write
semantics in §5.6. The flock concurrency test (test_concurrent_writers_flock)
is the load-bearing invariant: ids must stay monotonic under contention.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path

import pytest

from butterfly.runtime import events
from butterfly.runtime.events import (
    EVENT_AGENT_TEXT,
    EVENT_AGENT_THINKING,
    EVENT_AGENT_TOOL_CALL,
    EVENT_AGENT_TOOL_RESULT,
    EVENT_MODEL_STATUS,
    EVENT_SESSION_STARTED,
    EVENT_SYSTEM_NOTICE,
    EVENT_USER_INPUT,
    EVENT_USER_INTERRUPT,
    EVENTS_FILENAME,
    Event,
    append_event,
    latest_event_id,
    read_events,
    tail_events,
)


# ── append_event — ids and schema ─────────────────────────────────────────────

def test_append_assigns_monotonic_ids(tmp_path: Path) -> None:
    ev1 = append_event(tmp_path, EVENT_USER_INPUT, {"text": "a"})
    ev2 = append_event(tmp_path, EVENT_AGENT_TEXT, {"text": "b", "model": "x"})
    ev3 = append_event(tmp_path, EVENT_AGENT_TEXT, {"text": "c", "model": "x"})
    assert ev1.id == 1
    assert ev2.id == 2
    assert ev3.id == 3


def test_append_persists_exact_schema(tmp_path: Path) -> None:
    append_event(tmp_path, EVENT_USER_INPUT, {"text": "hi"}, ts=1.0)
    append_event(tmp_path, EVENT_MODEL_STATUS, {"status": "running", "model": "gpt"}, ts=2.0)

    path = tmp_path / EVENTS_FILENAME
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
    assert len(lines) == 2
    for line in lines:
        d = json.loads(line)
        assert set(d.keys()) == {"id", "ts", "type", "for_llm", "payload"}
        assert isinstance(d["id"], int)
        assert isinstance(d["ts"], (int, float))
        assert isinstance(d["type"], str)
        assert isinstance(d["for_llm"], bool)
        assert isinstance(d["payload"], dict)


# ── for_llm taxonomy ──────────────────────────────────────────────────────────

def test_for_llm_defaults_from_taxonomy(tmp_path: Path) -> None:
    # user-side → True
    assert append_event(tmp_path, EVENT_USER_INPUT, {"text": "a"}).for_llm is True
    assert append_event(tmp_path, EVENT_USER_INTERRUPT, {"text": None}).for_llm is True
    # agent-side → True
    assert append_event(tmp_path, EVENT_AGENT_TEXT, {"text": "x", "model": "m"}).for_llm is True
    assert append_event(tmp_path, EVENT_AGENT_THINKING, {"text": "th"}).for_llm is True
    assert append_event(
        tmp_path,
        EVENT_AGENT_TOOL_CALL,
        {"tool_use_id": "t1", "tool_name": "bash", "args": {}},
    ).for_llm is True
    assert append_event(
        tmp_path,
        EVENT_AGENT_TOOL_RESULT,
        {
            "tool_use_id": "t1",
            "tool_name": "bash",
            "result": "ok",
            "is_error": False,
            "is_background": False,
            "duration_ms": 1.0,
        },
    ).for_llm is True
    # system-side → False
    assert append_event(tmp_path, EVENT_MODEL_STATUS, {"status": "idle", "model": None}).for_llm is False
    assert append_event(tmp_path, EVENT_SESSION_STARTED, {}).for_llm is False
    assert append_event(
        tmp_path, EVENT_SYSTEM_NOTICE, {"text": "hi", "level": "info"}
    ).for_llm is False


def test_for_llm_override(tmp_path: Path) -> None:
    """Explicit for_llm=False on a user_input overrides the default True."""
    ev = append_event(tmp_path, EVENT_USER_INPUT, {"text": "hi"}, for_llm=False)
    assert ev.for_llm is False


def test_for_llm_unknown_type_defaults_false_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="butterfly.runtime.events"):
        ev = append_event(tmp_path, "weird_type", {"x": 1})
    assert ev.for_llm is False
    assert any("weird_type" in rec.message for rec in caplog.records)


# ── read_events filters ───────────────────────────────────────────────────────

def _seed_five(tmp_path: Path) -> list[Event]:
    out: list[Event] = []
    for i in range(5):
        kind = EVENT_USER_INPUT if i % 2 == 0 else EVENT_AGENT_TEXT
        payload = {"text": f"t{i}"}
        if kind == EVENT_AGENT_TEXT:
            payload["model"] = "m"
        out.append(append_event(tmp_path, kind, payload))
    return out


def test_read_events_filters_by_since_id(tmp_path: Path) -> None:
    _seed_five(tmp_path)
    got = list(read_events(tmp_path, since_id=2))
    assert [e.id for e in got] == [3, 4, 5]


def test_read_events_filters_by_until_id(tmp_path: Path) -> None:
    _seed_five(tmp_path)
    got = list(read_events(tmp_path, until_id=3))
    assert [e.id for e in got] == [1, 2, 3]


def test_read_events_filters_by_types(tmp_path: Path) -> None:
    _seed_five(tmp_path)
    got = list(read_events(tmp_path, types=[EVENT_USER_INPUT]))
    assert len(got) == 3
    assert all(e.type == EVENT_USER_INPUT for e in got)


def test_read_events_empty_file(tmp_path: Path) -> None:
    assert list(read_events(tmp_path)) == []


# ── latest_event_id ───────────────────────────────────────────────────────────

def test_latest_event_id_zero_when_missing(tmp_path: Path) -> None:
    assert latest_event_id(tmp_path) == 0


def test_latest_event_id_after_appends(tmp_path: Path) -> None:
    _seed_five(tmp_path)
    assert latest_event_id(tmp_path) == 5


# ── Concurrent writers — the flock invariant test ────────────────────────────

def test_concurrent_writers_flock(tmp_path: Path) -> None:
    """Two threads, each appending 50 events. All 100 ids must be unique and
    contiguous 1..100 — that's the flock invariant."""
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            for i in range(50):
                append_event(tmp_path, EVENT_USER_INPUT, {"text": f"x{i}"})
        except BaseException as e:  # pragma: no cover - collect and re-raise
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"worker errors: {errors}"

    path = tmp_path / EVENTS_FILENAME
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
    assert len(lines) == 100

    ids = [json.loads(ln)["id"] for ln in lines]
    assert sorted(ids) == list(range(1, 101))
    assert len(set(ids)) == 100


# ── tail_events ───────────────────────────────────────────────────────────────

def test_tail_events_yields_new_events(tmp_path: Path) -> None:
    # Seed one event so cursor starts at 1.
    append_event(tmp_path, EVENT_USER_INPUT, {"text": "seed"})

    poll = 0.05

    async def run() -> list[Event]:
        loop = asyncio.get_running_loop()

        # Schedule the writer on a thread so it doesn't contend with the
        # async tail.
        def writer() -> None:
            append_event(tmp_path, EVENT_AGENT_TEXT, {"text": "new", "model": "m"})

        loop.call_later(poll * 2, lambda: threading.Thread(target=writer).start())

        out: list[Event] = []
        async for ev in tail_events(
            tmp_path, cursor=1, timeout=poll * 20, poll_interval=poll
        ):
            out.append(ev)
            break  # first new event is enough
        return out

    got = asyncio.run(run())
    assert len(got) == 1
    assert got[0].id == 2
    assert got[0].type == EVENT_AGENT_TEXT


def test_tail_events_timeout(tmp_path: Path) -> None:
    """With no writes, tail_events returns within timeout."""
    poll = 0.05
    timeout = 0.3

    async def run() -> list[Event]:
        out: list[Event] = []
        async for ev in tail_events(tmp_path, cursor=0, timeout=timeout, poll_interval=poll):
            out.append(ev)
        return out

    import time as _t
    t0 = _t.monotonic()
    got = asyncio.run(run())
    elapsed = _t.monotonic() - t0
    assert got == []
    # Allow generous slack; the key is it exits, not hangs forever.
    assert elapsed < timeout + 1.0


# ── Event.to_dict / from_dict ────────────────────────────────────────────────

def test_event_to_dict_from_dict_roundtrip() -> None:
    ev = Event(
        id=7,
        ts=12345.678,
        type=EVENT_USER_INPUT,
        for_llm=True,
        payload={"text": "hi", "source": "cli"},
    )
    d = ev.to_dict()
    # Keys match exactly.
    assert set(d.keys()) == {"id", "ts", "type", "for_llm", "payload"}
    ev2 = Event.from_dict(d)
    assert ev2 == ev


# ── Determinism: explicit ts ──────────────────────────────────────────────────

def test_append_accepts_ts_for_determinism(tmp_path: Path) -> None:
    ev = append_event(tmp_path, EVENT_USER_INPUT, {"text": "hi"}, ts=1234.5)
    assert ev.ts == 1234.5

    path = tmp_path / EVENTS_FILENAME
    line = path.read_text(encoding="utf-8").strip()
    d = json.loads(line)
    assert d["ts"] == 1234.5


# ── Exhaustive taxonomy (DESIGN.md §3) ───────────────────────────────────────
#
# Every key in ``_FOR_LLM_DEFAULTS`` must round-trip through append +
# read with its taxonomy flag intact. This pin catches the drift failure
# mode where a new event type is added to the dict but its default isn't
# consciously set — the test fails with a clear list of offenders instead
# of each consumer learning about the mismatch at runtime.


def test_every_event_type_in_defaults_round_trips(tmp_path: Path) -> None:
    """For each entry in ``_FOR_LLM_DEFAULTS``, write one event and read
    it back; type, payload, and for_llm flag must survive verbatim."""
    from butterfly.runtime.events import _FOR_LLM_DEFAULTS

    # Event payload is deliberately minimal — we're testing the envelope,
    # not domain schema. A single marker field confirms round-trip.
    for i, (event_type, expected_for_llm) in enumerate(_FOR_LLM_DEFAULTS.items()):
        ev = append_event(
            tmp_path, event_type, {"_probe": i}, ts=1000.0 + i,
        )
        assert ev.type == event_type
        assert ev.for_llm is expected_for_llm
        assert ev.payload == {"_probe": i}

    all_events = list(read_events(tmp_path))
    assert len(all_events) == len(_FOR_LLM_DEFAULTS)
    for ev, (event_type, expected_for_llm) in zip(all_events, _FOR_LLM_DEFAULTS.items()):
        assert ev.type == event_type
        assert ev.for_llm is expected_for_llm


def test_for_llm_flag_matches_design_taxonomy() -> None:
    """DESIGN.md §3.3-§3.5 table: user-side + agent-side events are
    for_llm=True; system-side are False. Enforce that partition rather
    than hand-picking each type (catches the flip-a-default bug)."""
    from butterfly.runtime.events import (
        _FOR_LLM_DEFAULTS,
        EVENT_AGENT_TEXT,
        EVENT_AGENT_THINKING,
        EVENT_AGENT_TOOL_CALL,
        EVENT_AGENT_TOOL_RESULT,
        EVENT_USER_INPUT,
        EVENT_USER_INTERRUPT,
    )
    llm_types = {
        EVENT_USER_INPUT,
        EVENT_USER_INTERRUPT,
        EVENT_AGENT_TEXT,
        EVENT_AGENT_THINKING,
        EVENT_AGENT_TOOL_CALL,
        EVENT_AGENT_TOOL_RESULT,
    }
    # Every in-list type is True; every other registered type is False.
    for t, flag in _FOR_LLM_DEFAULTS.items():
        if t in llm_types:
            assert flag is True, f"{t!r} should be for_llm=True"
        else:
            assert flag is False, f"{t!r} should be for_llm=False"
