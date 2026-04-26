"""Tests for butterfly.runtime.llm_context — Phase 2.

Covers the contract in DESIGN.md §4: role mapping, grouping, skipping of
system events, the user_interrupt subtlety (control signal vs user turn),
and the money alignment test (I7) that live and disk-round-trip produce
the same messages.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from butterfly.runtime.events import (
    EVENT_AGENT_TEXT,
    EVENT_AGENT_THINKING,
    EVENT_AGENT_TOOL_CALL,
    EVENT_AGENT_TOOL_RESULT,
    EVENT_LLM_CALL_USAGE,
    EVENT_MODEL_STATUS,
    EVENT_TASK_CARD_CHANGED,
    EVENT_TOOL_PROGRESS,
    EVENT_USER_INPUT,
    EVENT_USER_INTERRUPT,
    Event,
    append_event,
    read_events,
)
from butterfly.runtime.llm_context import (
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    build_llm_context,
    read_llm_context,
)


# ── Small helpers to build events directly (no disk IO) ───────────────────────

_ID = [0]


def _next_id() -> int:
    _ID[0] += 1
    return _ID[0]


def _reset_ids() -> None:
    _ID[0] = 0


def _ev(event_type: str, payload: dict, *, for_llm: bool = True, ts: float | None = None) -> Event:
    return Event(
        id=_next_id(),
        ts=ts if ts is not None else float(_next_id()),  # monotonic-ish
        type=event_type,
        for_llm=for_llm,
        payload=payload,
    )


@pytest.fixture(autouse=True)
def _fresh_ids() -> None:
    _reset_ids()


# ── Basic shape ───────────────────────────────────────────────────────────────

def test_empty_events_yields_empty_messages() -> None:
    assert build_llm_context([]) == []


def test_single_user_input_yields_user_text_message() -> None:
    events = [_ev(EVENT_USER_INPUT, {"text": "hello", "source": "cli"})]
    msgs = build_llm_context(events)
    assert msgs == [Message(role="user", content=(TextBlock(text="hello"),))]


def test_agent_text_after_user_starts_assistant_message() -> None:
    events = [
        _ev(EVENT_USER_INPUT, {"text": "hi"}),
        _ev(EVENT_AGENT_TEXT, {"text": "hello back", "model": "m"}),
    ]
    msgs = build_llm_context(events)
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[1].role == "assistant"
    assert msgs[0].content == (TextBlock(text="hi"),)
    assert msgs[1].content == (TextBlock(text="hello back"),)


# ── Grouping (DESIGN.md §4.3) ─────────────────────────────────────────────────

def test_adjacent_user_inputs_merge_into_one_message() -> None:
    events = [
        _ev(EVENT_USER_INPUT, {"text": "a"}),
        _ev(EVENT_USER_INPUT, {"text": "b"}),
        _ev(EVENT_USER_INPUT, {"text": "c"}),
    ]
    msgs = build_llm_context(events)
    assert len(msgs) == 1
    assert msgs[0].role == "user"
    assert msgs[0].content == (
        TextBlock(text="a"),
        TextBlock(text="b"),
        TextBlock(text="c"),
    )


def test_role_switch_splits_messages() -> None:
    events = [
        _ev(EVENT_USER_INPUT, {"text": "u1"}),
        _ev(EVENT_AGENT_TEXT, {"text": "a1", "model": "m"}),
        _ev(EVENT_USER_INPUT, {"text": "u2"}),
        _ev(EVENT_AGENT_TEXT, {"text": "a2", "model": "m"}),
    ]
    msgs = build_llm_context(events)
    assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0].content == (TextBlock(text="u1"),)
    assert msgs[2].content == (TextBlock(text="u2"),)


def test_thinking_and_text_in_same_assistant_message() -> None:
    events = [
        _ev(EVENT_AGENT_THINKING, {"text": "reasoning"}),
        _ev(EVENT_AGENT_TEXT, {"text": "answer", "model": "m"}),
    ]
    msgs = build_llm_context(events)
    assert len(msgs) == 1
    assert msgs[0].role == "assistant"
    assert msgs[0].content == (
        ThinkingBlock(text="reasoning"),
        TextBlock(text="answer"),
    )


def test_multiple_tool_calls_and_results_group_correctly() -> None:
    """The canonical pattern from DESIGN.md §4.3."""
    events = [
        _ev(EVENT_USER_INPUT, {"text": "do it"}),
        _ev(EVENT_AGENT_THINKING, {"text": "planning"}),
        _ev(EVENT_AGENT_TEXT, {"text": "working...", "model": "m"}),
        _ev(EVENT_AGENT_TOOL_CALL, {"tool_use_id": "t1", "tool_name": "bash", "args": {"cmd": "ls"}}),
        _ev(EVENT_AGENT_TOOL_CALL, {"tool_use_id": "t2", "tool_name": "bash", "args": {"cmd": "pwd"}}),
        _ev(
            EVENT_AGENT_TOOL_RESULT,
            {"tool_use_id": "t1", "tool_name": "bash", "result": "file1", "is_error": False,
             "is_background": False, "duration_ms": 1.0},
        ),
        _ev(
            EVENT_AGENT_TOOL_RESULT,
            {"tool_use_id": "t2", "tool_name": "bash", "result": "/tmp", "is_error": False,
             "is_background": False, "duration_ms": 1.0},
        ),
        _ev(EVENT_AGENT_TEXT, {"text": "done", "model": "m"}),
    ]
    msgs = build_llm_context(events)
    assert len(msgs) == 4
    assert msgs[0].role == "user"
    assert msgs[0].content == (TextBlock(text="do it"),)
    assert msgs[1].role == "assistant"
    assert msgs[1].content == (
        ThinkingBlock(text="planning"),
        TextBlock(text="working...", ),
        ToolUseBlock(id="t1", name="bash", input={"cmd": "ls"}),
        ToolUseBlock(id="t2", name="bash", input={"cmd": "pwd"}),
    )
    assert msgs[2].role == "user"
    assert msgs[2].content == (
        ToolResultBlock(tool_use_id="t1", content="file1", is_error=False),
        ToolResultBlock(tool_use_id="t2", content="/tmp", is_error=False),
    )
    assert msgs[3].role == "assistant"
    assert msgs[3].content == (TextBlock(text="done"),)


# ── System events are dropped ─────────────────────────────────────────────────

def test_system_events_are_dropped() -> None:
    """System events sitting between user+agent events must NOT break grouping."""
    events = [
        _ev(EVENT_USER_INPUT, {"text": "u1"}),
        _ev(EVENT_MODEL_STATUS, {"status": "running", "model": "m"}, for_llm=False),
        _ev(EVENT_USER_INPUT, {"text": "u2"}),
        _ev(EVENT_LLM_CALL_USAGE, {"iteration": 1, "usage": {}, "context_tokens": 10,
                                   "toks_per_s": 0.0, "duration_ms": 0.0}, for_llm=False),
        _ev(EVENT_AGENT_TEXT, {"text": "a1", "model": "m"}),
        _ev(EVENT_TASK_CARD_CHANGED, {"name": "t", "card": {}}, for_llm=False),
        _ev(EVENT_TOOL_PROGRESS, {"tool_use_id": "x", "text": "...", "kind": "stdout"},
            for_llm=False),
        _ev(EVENT_AGENT_TEXT, {"text": "a2", "model": "m"}),
    ]
    msgs = build_llm_context(events)
    # Two user_inputs merge; two agent_texts merge — system events don't break.
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[0].content == (TextBlock(text="u1"), TextBlock(text="u2"))
    assert msgs[1].role == "assistant"
    assert msgs[1].content == (TextBlock(text="a1"), TextBlock(text="a2"))


# ── user_interrupt subtleties ─────────────────────────────────────────────────

def test_user_interrupt_with_text_becomes_user_message() -> None:
    events = [
        _ev(EVENT_AGENT_TEXT, {"text": "thinking aloud", "model": "m"}),
        _ev(EVENT_USER_INTERRUPT, {"text": "stop that"}),
        _ev(EVENT_AGENT_TEXT, {"text": "ok", "model": "m"}),
    ]
    msgs = build_llm_context(events)
    assert [m.role for m in msgs] == ["assistant", "user", "assistant"]
    assert msgs[1].content == (TextBlock(text="stop that"),)


def test_user_interrupt_without_text_is_skipped() -> None:
    """Control-signal interrupts (text=None or empty) are absent from the
    context AND must not break grouping of surrounding events."""
    # text=None
    events = [
        _ev(EVENT_AGENT_TEXT, {"text": "a1", "model": "m"}),
        _ev(EVENT_USER_INTERRUPT, {"text": None}),
        _ev(EVENT_AGENT_TEXT, {"text": "a2", "model": "m"}),
    ]
    msgs = build_llm_context(events)
    # Both assistant events should merge into ONE message — the skipped
    # interrupt must not split the role run.
    assert len(msgs) == 1
    assert msgs[0].role == "assistant"
    assert msgs[0].content == (TextBlock(text="a1"), TextBlock(text="a2"))

    # text="" (empty string) — same treatment.
    _reset_ids()
    events2 = [
        _ev(EVENT_AGENT_TEXT, {"text": "a1", "model": "m"}),
        _ev(EVENT_USER_INTERRUPT, {"text": ""}),
        _ev(EVENT_AGENT_TEXT, {"text": "a2", "model": "m"}),
    ]
    msgs2 = build_llm_context(events2)
    assert len(msgs2) == 1
    assert msgs2[0].role == "assistant"
    assert msgs2[0].content == (TextBlock(text="a1"), TextBlock(text="a2"))


# ── Thinking block fidelity ───────────────────────────────────────────────────

def test_agent_thinking_block_preserves_signature_and_summary() -> None:
    events = [
        _ev(EVENT_AGENT_THINKING, {
            "text": "reasoning",
            "signature": "sig-abc",
            "summary": "short summary",
            "redacted": False,
        }),
    ]
    msgs = build_llm_context(events)
    assert len(msgs) == 1
    assert msgs[0].content == (
        ThinkingBlock(
            text="reasoning",
            signature="sig-abc",
            summary="short summary",
            redacted=False,
        ),
    )


def test_agent_thinking_interrupted_still_included() -> None:
    """interrupted=True in payload is metadata only — the block still appears,
    with whatever partial text was captured preserved verbatim."""
    events = [
        _ev(EVENT_AGENT_THINKING, {
            "text": "partial reasoning",
            "interrupted": True,
            "reasoning_tokens": 5,
            "duration_ms": 12.0,
        }),
    ]
    msgs = build_llm_context(events)
    assert len(msgs) == 1
    assert len(msgs[0].content) == 1
    block = msgs[0].content[0]
    assert isinstance(block, ThinkingBlock)
    assert block.text == "partial reasoning"
    # Block type has no "interrupted" field — metadata does not leak into IR.
    assert not hasattr(block, "interrupted")


# ── Tool call/result fidelity ─────────────────────────────────────────────────

def test_agent_tool_call_input_shape() -> None:
    args = {"cmd": "ls -la", "cwd": "/tmp", "flags": ["x", "y"], "n": 3}
    events = [
        _ev(EVENT_AGENT_TOOL_CALL, {
            "tool_use_id": "tool-42",
            "tool_name": "bash",
            "args": args,
        }),
    ]
    msgs = build_llm_context(events)
    block = msgs[0].content[0]
    assert isinstance(block, ToolUseBlock)
    assert block.id == "tool-42"
    assert block.name == "bash"
    assert block.input == args


def test_agent_tool_result_content_is_string() -> None:
    """Non-string `result` payloads coerce to string via str() (bytes decoded
    via UTF-8 with 'replace' errors). is_error preserved verbatim."""
    # int result
    events = [_ev(EVENT_AGENT_TOOL_RESULT, {
        "tool_use_id": "t1", "tool_name": "calc", "result": 42,
        "is_error": False, "is_background": False, "duration_ms": 1.0,
    })]
    msgs = build_llm_context(events)
    block = msgs[0].content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.content == "42"
    assert block.is_error is False

    # dict result
    _reset_ids()
    events = [_ev(EVENT_AGENT_TOOL_RESULT, {
        "tool_use_id": "t2", "tool_name": "x", "result": {"k": 1},
        "is_error": True, "is_background": False, "duration_ms": 1.0,
    })]
    msgs = build_llm_context(events)
    block = msgs[0].content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.content == "{'k': 1}"
    assert block.is_error is True

    # bytes result
    _reset_ids()
    events = [_ev(EVENT_AGENT_TOOL_RESULT, {
        "tool_use_id": "t3", "tool_name": "x", "result": b"hello bytes",
        "is_error": False, "is_background": False, "duration_ms": 1.0,
    })]
    msgs = build_llm_context(events)
    block = msgs[0].content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.content == "hello bytes"


# ── Schema integrity ──────────────────────────────────────────────────────────

def test_unknown_for_llm_type_raises() -> None:
    weird = Event(
        id=1,
        ts=1.0,
        type="weird_unknown",
        for_llm=True,
        payload={"x": 1},
    )
    with pytest.raises(ValueError, match="weird_unknown"):
        build_llm_context([weird])


# ── Purity & ordering ─────────────────────────────────────────────────────────

def test_builder_is_pure() -> None:
    events = [
        _ev(EVENT_USER_INPUT, {"text": "u"}),
        _ev(EVENT_AGENT_THINKING, {"text": "t"}),
        _ev(EVENT_AGENT_TEXT, {"text": "a", "model": "m"}),
    ]
    once = build_llm_context(events)
    twice = build_llm_context(events)
    assert once == twice
    assert [m.to_dict() for m in once] == [m.to_dict() for m in twice]


def test_order_preserved_within_message() -> None:
    events = [_ev(EVENT_USER_INPUT, {"text": f"line-{i}"}) for i in range(5)]
    msgs = build_llm_context(events)
    assert len(msgs) == 1
    assert msgs[0].content == tuple(TextBlock(text=f"line-{i}") for i in range(5))


# ── The money test: I7 live=replay ────────────────────────────────────────────

def test_alignment_live_equals_replay(tmp_path: Path) -> None:
    """Build 15 events covering every for_llm type + several system events,
    persist them via `append_event` (disk round-trip), then compare:

      (a) replay path:  read_events(tmp_path) → build_llm_context
      (b) live path:    feed the same objects one-at-a-time to build_llm_context

    Both must produce byte-for-byte identical messages. This is invariant I7.
    """
    # Build the canonical mixed sequence on disk.
    append_event(tmp_path, EVENT_USER_INPUT, {"text": "first question", "source": "cli"})
    append_event(tmp_path, EVENT_MODEL_STATUS, {"status": "running", "model": "m"})
    append_event(tmp_path, EVENT_AGENT_THINKING, {
        "text": "think step 1", "signature": "sig1", "summary": "s1",
        "redacted": False, "interrupted": False, "reasoning_tokens": 3,
        "duration_ms": 1.0,
    })
    append_event(tmp_path, EVENT_AGENT_TEXT, {"text": "let me check", "model": "m"})
    append_event(tmp_path, EVENT_AGENT_TOOL_CALL, {
        "tool_use_id": "tc1", "tool_name": "bash", "args": {"cmd": "ls"},
    })
    append_event(tmp_path, EVENT_TOOL_PROGRESS, {
        "tool_use_id": "tc1", "text": "loading...", "kind": "status",
    })
    append_event(tmp_path, EVENT_AGENT_TOOL_RESULT, {
        "tool_use_id": "tc1", "tool_name": "bash", "result": "file1 file2",
        "is_error": False, "is_background": False, "duration_ms": 2.0,
    })
    append_event(tmp_path, EVENT_LLM_CALL_USAGE, {
        "iteration": 1, "usage": {"input": 10, "output": 5},
        "context_tokens": 15, "toks_per_s": 7.5, "duration_ms": 666.0,
    })
    append_event(tmp_path, EVENT_USER_INTERRUPT, {"text": "hold up"})
    append_event(tmp_path, EVENT_USER_INTERRUPT, {"text": None})  # pure control
    append_event(tmp_path, EVENT_AGENT_THINKING, {"text": "re-plan"})
    append_event(tmp_path, EVENT_AGENT_TEXT, {"text": "sure", "model": "m"})
    append_event(tmp_path, EVENT_AGENT_TOOL_CALL, {
        "tool_use_id": "tc2", "tool_name": "web_search", "args": {"q": "hi"},
    })
    append_event(tmp_path, EVENT_AGENT_TOOL_RESULT, {
        "tool_use_id": "tc2", "tool_name": "web_search", "result": {"hits": [1, 2]},
        "is_error": False, "is_background": False, "duration_ms": 3.0,
    })
    append_event(tmp_path, EVENT_TASK_CARD_CHANGED, {"name": "tk", "card": {"a": 1}})

    all_events = list(read_events(tmp_path))
    assert len(all_events) == 15

    # Replay path (a)
    replay_msgs = build_llm_context(all_events)

    # Live path (b): iterate over the same list exactly once, wrapped in iter()
    # to simulate streaming arrival.
    live_msgs = build_llm_context(iter(all_events))

    assert replay_msgs == live_msgs
    assert [m.to_dict() for m in replay_msgs] == [m.to_dict() for m in live_msgs]

    # Spot-check that system events were dropped.
    for msg in replay_msgs:
        for block in msg.content:
            assert block.type in ("text", "thinking", "tool_use", "tool_result")

    # Also verify the convenience wrapper gives the same result.
    via_disk = read_llm_context(tmp_path)
    assert via_disk == replay_msgs


# ── Wire shape ────────────────────────────────────────────────────────────────

def test_to_dict_shape_anthropic_compatible() -> None:
    events = [
        _ev(EVENT_USER_INPUT, {"text": "hi"}),
        _ev(EVENT_AGENT_THINKING, {
            "text": "think",
            "signature": "sig",
            "summary": None,
            "redacted": False,
        }),
        _ev(EVENT_AGENT_TOOL_CALL, {
            "tool_use_id": "x1", "tool_name": "bash", "args": {"cmd": "ls"},
        }),
    ]
    msgs = build_llm_context(events)
    assert [m.to_dict() for m in msgs] == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "hi"}],
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "text": "think",
                    "signature": "sig",
                    "summary": None,
                    "redacted": False,
                },
                {
                    "type": "tool_use",
                    "id": "x1",
                    "name": "bash",
                    "input": {"cmd": "ls"},
                },
            ],
        },
    ]
