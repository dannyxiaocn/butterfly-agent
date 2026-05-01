"""Unit tests for OpenAIResponsesProvider — conversion + helpers."""
from __future__ import annotations

from types import SimpleNamespace

from butterfly.core.types import Message
from butterfly.llm_engine.providers.openai_responses import (
    OpenAIResponsesProvider,
    _capture_reasoning,
    _convert_assistant,
    _convert_messages,
    _extract_usage_from_obj,
    _tool_to_responses,
)


def test_registry_has_openai_responses():
    from butterfly.llm_engine.registry import _REGISTRY
    assert "openai-responses" in _REGISTRY
    mod, cls = _REGISTRY["openai-responses"]
    assert cls == "OpenAIResponsesProvider"


def test_tool_schema_is_flat_no_inner_function_wrapper():
    class _T:
        def to_api_dict(self):
            return {"name": "search", "description": "search", "input_schema": {"type": "object"}}

    schema = _tool_to_responses(_T())
    assert schema["type"] == "function"
    assert schema["name"] == "search"
    assert "function" not in schema  # flat, not the Chat Completions shape
    assert schema["strict"] is False


def test_convert_assistant_emits_reasoning_before_text():
    msg = Message(
        role="assistant",
        content=[
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "X"},
            {"type": "text", "text": "final"},
        ],
    )
    items = _convert_assistant(msg)
    assert items[0]["type"] == "reasoning"
    assert items[0]["encrypted_content"] == "X"
    assert items[1]["type"] == "message"


def test_convert_messages_user_string_becomes_input_text():
    items = _convert_messages([Message(role="user", content="hi")])
    assert items == [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]


def test_capture_reasoning_preserves_encrypted_content():
    item = {
        "type": "reasoning",
        "id": "rs_99",
        "summary": [{"type": "summary_text", "text": "s"}],
        "encrypted_content": "OPAQUE",
    }
    out = _capture_reasoning(item)
    assert out == item


def test_capture_reasoning_omits_encrypted_content_when_absent():
    item = {"type": "reasoning", "id": "rs_99", "summary": []}
    out = _capture_reasoning(item)
    assert "encrypted_content" not in out


def test_extract_usage_from_obj_handles_reasoning_and_cache():
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=200,
        input_tokens_details=SimpleNamespace(cached_tokens=20),
        output_tokens_details=SimpleNamespace(reasoning_tokens=100),
    )
    u = _extract_usage_from_obj(usage)
    assert u.input_tokens == 80
    assert u.output_tokens == 200
    assert u.cache_read_tokens == 20
    assert u.reasoning_tokens == 100


def test_extract_usage_from_obj_none_returns_zero():
    u = _extract_usage_from_obj(None)
    assert u.input_tokens == 0
    assert u.output_tokens == 0


def test_consume_extra_blocks_drains():
    p = OpenAIResponsesProvider.__new__(OpenAIResponsesProvider)
    p._pending_reasoning = [{"type": "reasoning", "id": "rs_a"}]
    p._pending_builtin_items = []
    p._pending_builtin_progress = []
    assert p.consume_extra_blocks() == [{"type": "reasoning", "id": "rs_a"}]
    assert p.consume_extra_blocks() == []


# ── tool result conversion ──────────────────────────────────────────


def test_convert_tool_result_string_content():
    from butterfly.llm_engine.providers.openai_responses import _convert_tool_result

    msg = Message(
        role="tool",
        content=[{"type": "tool_result", "tool_use_id": "tc-1", "content": "ok"}],
    )
    items = _convert_tool_result(msg)
    assert items == [{"type": "function_call_output", "call_id": "tc-1", "output": "ok"}]


def test_convert_tool_result_list_content_joined():
    from butterfly.llm_engine.providers.openai_responses import _convert_tool_result

    msg = Message(
        role="tool",
        content=[{
            "type": "tool_result",
            "tool_use_id": "tc-2",
            "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        }],
    )
    items = _convert_tool_result(msg)
    assert items[0]["call_id"] == "tc-2"
    # Multi-text blocks are byte-for-byte concatenated (matches Anthropic
    # convention). Space-joining was BUG-8.
    assert items[0]["output"] == "ab"


def test_convert_tool_result_wraps_string_content():
    """Bug 20 fix: string-typed tool content is wrapped, not dropped."""
    from butterfly.llm_engine.providers.openai_responses import _convert_tool_result

    msg = Message(role="tool", content="flat")
    out = _convert_tool_result(msg)
    assert out == [{"type": "function_call_output", "call_id": "", "output": "flat"}]


# ── non-streaming response parsing ──────────────────────────────────


def test_parse_response_object_extracts_text_tools_reasoning():
    from butterfly.llm_engine.providers.openai_responses import _parse_response_object

    response = SimpleNamespace(
        output=[
            {"type": "message", "content": [{"type": "output_text", "text": "hello"}]},
            {
                "type": "function_call",
                "call_id": "tc-7",
                "name": "search",
                "arguments": '{"q": "x"}',
            },
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [],
                "encrypted_content": "OPAQUE",
            },
        ],
        usage=SimpleNamespace(
            input_tokens=12,
            output_tokens=6,
            input_tokens_details=SimpleNamespace(cached_tokens=2),
            output_tokens_details=SimpleNamespace(reasoning_tokens=3),
        ),
    )
    pending: list = []
    text, tool_calls, usage = _parse_response_object(response, pending=pending)

    assert text == "hello"
    assert len(tool_calls) == 1
    assert tool_calls[0].id == "tc-7"
    assert tool_calls[0].input == {"q": "x"}
    assert usage.input_tokens == 10
    assert usage.reasoning_tokens == 3
    assert len(pending) == 1
    assert pending[0]["encrypted_content"] == "OPAQUE"


def test_parse_response_object_accepts_model_dump_items():
    """Items may arrive as pydantic-like objects exposing model_dump()."""
    from butterfly.llm_engine.providers.openai_responses import _parse_response_object

    message_item = SimpleNamespace(
        model_dump=lambda exclude_none=True: {
            "type": "message",
            "content": [{"type": "output_text", "text": "dumped"}],
        }
    )
    response = SimpleNamespace(output=[message_item], usage=None)
    text, tool_calls, usage = _parse_response_object(response, pending=[])
    assert text == "dumped"
    assert tool_calls == []
    assert usage.input_tokens == 0


# ── full messages round-trip ────────────────────────────────────────


def test_convert_messages_full_round_trip_with_tool_result():
    messages = [
        Message(role="user", content="hi"),
        Message(role="assistant", content=[
            {"type": "reasoning", "id": "rs_k", "encrypted_content": "E"},
            {"type": "text", "text": "working"},
            {"type": "tool_use", "id": "t1", "name": "echo", "input": {"x": 1}},
        ]),
        Message(role="tool", content=[
            {"type": "tool_result", "tool_use_id": "t1", "content": "done"},
        ]),
    ]
    items = _convert_messages(messages)
    kinds = [i.get("type") or i.get("role") for i in items]
    # user message → reasoning → text message → function_call → function_call_output
    assert kinds == ["user", "reasoning", "message", "function_call", "function_call_output"]
    assert items[1]["encrypted_content"] == "E"
    assert items[3]["call_id"] == "t1"
    assert items[4]["output"] == "done"


# ── BUG-5 regression: thinking_effort="none" honored (no reasoning block sent) ──


def test_valid_efforts_includes_none():
    from butterfly.llm_engine.providers.openai_responses import _VALID_EFFORTS
    assert "none" in _VALID_EFFORTS


import pytest


@pytest.mark.asyncio
async def test_complete_with_effort_none_omits_reasoning_block(monkeypatch):
    """thinking=True but thinking_effort="none" must NOT send reasoning=..."""
    from butterfly.llm_engine.providers import openai_responses as mod

    provider = OpenAIResponsesProvider.__new__(OpenAIResponsesProvider)
    provider.max_tokens = 100
    provider._conversation_id = "test-conv"
    provider._pending_reasoning = []
    provider._pending_builtin_items = []
    provider._pending_builtin_progress = []

    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)

        class _Response:
            output = []
            usage = None

        return _Response()

    class _Client:
        class responses:
            @staticmethod
            async def create(**kwargs):
                return await fake_create(**kwargs)

    provider._client = _Client()

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="",
        model="gpt-5",
        thinking=True,
        thinking_effort="none",
    )

    assert "reasoning" not in captured
    assert "include" not in captured

import json
from types import SimpleNamespace
from typing import Any

import pytest

from butterfly.core.types import Message, TokenUsage
from butterfly.llm_engine.providers.openai_responses import (
    OpenAIResponsesProvider,
    _convert_tool_result,
)


# ── helpers ──────────────────────────────────────────────────────────


def _make_provider() -> OpenAIResponsesProvider:
    """Create a provider without hitting the real OpenAI SDK constructor."""
    p = OpenAIResponsesProvider.__new__(OpenAIResponsesProvider)
    p.max_tokens = 8096
    p._conversation_id = "conv-123"
    p._pending_reasoning = []
    p._pending_builtin_items = []
    p._pending_builtin_progress = []
    p._client = None  # patched per-test
    return p


class _FakeResponsesStreamCtxMgr:
    """Mimics openai.responses.stream() async context manager."""

    def __init__(self, events: list[Any], final_response: Any) -> None:
        self._events = events
        self._final_response = final_response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __aiter__(self):
        async def _gen():
            for e in self._events:
                yield e
        return _gen()

    async def get_final_response(self):
        return self._final_response


def _stream_event(etype: str, **fields) -> SimpleNamespace:
    """Build a fake stream event (openai SDK objects are namespace-like)."""
    return SimpleNamespace(type=etype, **fields)


def _make_final_response(
    output: list[Any] | None = None,
    usage: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        output=output or [],
        usage=usage or SimpleNamespace(
            input_tokens=0,
            output_tokens=0,
            input_tokens_details=None,
            output_tokens_details=None,
        ),
    )


# ======================================================================
# 1. Non-stream reasoning accumulation bug
# ======================================================================


@pytest.mark.asyncio
async def test_non_stream_reasoning_does_not_accumulate():
    """Post-fix (Bug 19): non-stream path replaces _pending_reasoning on each
    call instead of appending, so two back-to-back complete() calls without an
    intervening consume_extra_blocks() do not accumulate."""
    """
    _non_stream appends reasoning items to self._pending_reasoning (via the
    `pending` parameter). If complete() is called multiple times without
    consume_extra_blocks() in between, reasoning items accumulate.
    
    _stream REPLACES self._pending_reasoning, so the two paths are inconsistent.
    """
    provider = _make_provider()

    resp1 = SimpleNamespace(
        output=[
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "A"},
            {"type": "message", "content": [{"type": "output_text", "text": "hi"}]},
        ],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            input_tokens_details=None,
            output_tokens_details=None,
        ),
    )
    resp2 = SimpleNamespace(
        output=[
            {"type": "reasoning", "id": "rs_2", "summary": [], "encrypted_content": "B"},
            {"type": "message", "content": [{"type": "output_text", "text": "bye"}]},
        ],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            input_tokens_details=None,
            output_tokens_details=None,
        ),
    )

    calls = [resp1, resp2]

    async def _create(**kw):
        return calls.pop(0)

    provider._client = SimpleNamespace(responses=SimpleNamespace(create=_create))

    # First non-stream call
    text1, _, _ = await provider._non_stream({"model": "gpt-5"})
    assert text1 == "hi"
    assert len(provider._pending_reasoning) == 1
    assert provider._pending_reasoning[0]["id"] == "rs_1"

    # Second non-stream call WITHOUT consume_extra_blocks()
    text2, _, _ = await provider._non_stream({"model": "gpt-5"})
    assert text2 == "bye"
    # Post-fix (Bug 19): _non_stream replaces _pending_reasoning on each call
    # rather than appending, matching the _stream path. So only the newest
    # reasoning item survives.
    assert len(provider._pending_reasoning) == 1
    assert provider._pending_reasoning[0]["id"] == "rs_2"

@pytest.mark.asyncio
async def test_stream_replaces_reasoning_instead_of_appending():
    """
    Streaming path REPLACES self._pending_reasoning, which is inconsistent
    with the non-stream path that APPENDS.
    """
    provider = _make_provider()
    provider._pending_reasoning = [{"type": "reasoning", "id": "rs_old"}]

    events = [
        _stream_event(
            "response.output_item.done",
            item={"type": "reasoning", "id": "rs_new", "summary": []},
        ),
    ]
    final = _make_final_response()

    provider._client = SimpleNamespace(
        responses=SimpleNamespace(
            stream=lambda **kw: _FakeResponsesStreamCtxMgr(events, final)
        )
    )

    chunks: list[str] = []
    await provider._stream({"model": "gpt-5"}, chunks.append)

    # Streaming replaces the list entirely
    assert provider._pending_reasoning == [{"type": "reasoning", "id": "rs_new", "summary": []}]


# ======================================================================
# 2. Streaming errors swallowed
# ======================================================================


@pytest.mark.asyncio
async def test_stream_raises_on_response_incomplete_event():
    """Post-fix (Bug 17): `_stream` routes `response.incomplete` into the
    butterfly error taxonomy instead of silently completing."""
    from butterfly.llm_engine.errors import ProviderError
    provider = _make_provider()

    events = [
        _stream_event("response.output_text.delta", delta="hello "),
        _stream_event(
            "response.incomplete",
            response=SimpleNamespace(
                incomplete_details=SimpleNamespace(reason="max_output_tokens")
            ),
        ),
    ]
    final = _make_final_response(
        output=[{"type": "message", "content": [{"type": "output_text", "text": "hello "}]}],
    )

    provider._client = SimpleNamespace(
        responses=SimpleNamespace(
            stream=lambda **kw: _FakeResponsesStreamCtxMgr(events, final)
        )
    )

    chunks: list[str] = []
    with pytest.raises(ProviderError):
        await provider._stream({"model": "gpt-5"}, chunks.append)


@pytest.mark.asyncio
async def test_stream_raises_on_response_failed_event():
    """Post-fix (Bug 17): `_stream` routes `response.failed` through the
    butterfly error taxonomy (ProviderError or subclass) rather than
    dropping it."""
    from butterfly.llm_engine.errors import ProviderError
    provider = _make_provider()

    events = [
        _stream_event("response.output_text.delta", delta="hi"),
        _stream_event(
            "response.failed",
            response=SimpleNamespace(
                error=SimpleNamespace(code="rate_limit_exceeded", message="too fast")
            ),
        ),
    ]
    final = _make_final_response()

    provider._client = SimpleNamespace(
        responses=SimpleNamespace(
            stream=lambda **kw: _FakeResponsesStreamCtxMgr(events, final)
        )
    )

    chunks: list[str] = []
    with pytest.raises(ProviderError):
        await provider._stream({"model": "gpt-5"}, chunks.append)


@pytest.mark.asyncio
async def test_stream_swallows_generic_error_event():
    """
    _stream does NOT handle generic `error` events.
    The event is silently ignored instead of raising an error.
    """
    provider = _make_provider()

    events = [
        _stream_event("response.output_text.delta", delta="hi"),
        _stream_event("error", message="something broke", code="unknown"),
    ]
    final = _make_final_response()

    provider._client = SimpleNamespace(
        responses=SimpleNamespace(
            stream=lambda **kw: _FakeResponsesStreamCtxMgr(events, final)
        )
    )

    from butterfly.llm_engine.errors import ProviderError
    chunks: list[str] = []
    with pytest.raises(ProviderError):
        await provider._stream({"model": "gpt-5"}, chunks.append)


# ======================================================================
# 3. No error taxonomy integration
# ======================================================================


@pytest.mark.asyncio
async def test_non_stream_raises_raw_openai_exception_no_taxonomy():
    """
    _non_stream lets raw openai exceptions bubble up instead of mapping them
    to the butterfly llm_engine.errors taxonomy.
    """
    provider = _make_provider()

    class FakeAPIError(Exception):
        pass

    async def _create(**kw):
        raise FakeAPIError("raw openai error")

    provider._client = SimpleNamespace(responses=SimpleNamespace(create=_create))

    # BUG: raw exception is raised, not mapped to ProviderError/AuthError/etc.
    with pytest.raises(FakeAPIError):
        await provider._non_stream({"model": "gpt-5"})


@pytest.mark.asyncio
async def test_stream_raises_raw_openai_exception_no_taxonomy():
    """
    _stream lets raw openai exceptions bubble up instead of mapping them
    to the butterfly llm_engine.errors taxonomy.
    """
    provider = _make_provider()

    class FakeStreamError(Exception):
        pass

    class _BrokenStreamCtxMgr:
        async def __aenter__(self):
            raise FakeStreamError("raw stream error")
        async def __aexit__(self, *args):
            pass

    provider._client = SimpleNamespace(
        responses=SimpleNamespace(stream=lambda **kw: _BrokenStreamCtxMgr())
    )

    # BUG: raw exception is raised, not mapped to ProviderError/TimeoutError/etc.
    with pytest.raises(FakeStreamError):
        await provider._stream({"model": "gpt-5"}, lambda x: None)


# ======================================================================
# 4. Tool-result joining inconsistent
# ======================================================================


def test_tool_result_list_content_concatenated_without_space():
    """
    _convert_tool_result joins list content with " " (space).
    
    For comparison, OpenAIProvider (openai_api.py) uses "" (empty string)
    in _stringify_tool_result. This inconsistency means tool results sent
    to the Responses API have spaces inserted between text blocks, which
    may alter the meaning of concatenated output (e.g. "ab" vs "a b").
    """
    msg = Message(
        role="tool",
        content=[{
            "type": "tool_result",
            "tool_use_id": "tc-1",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "world"},
            ],
        }],
    )
    items = _convert_tool_result(msg)
    assert len(items) == 1
    # Post-fix (Bug 30): unified serialization concatenates text blocks
    # without a separator, matching Codex and OpenAI Chat Completions.
    assert items[0]["output"] == "helloworld"


# ======================================================================
# 5. Comprehensive streaming tests
# ======================================================================


@pytest.mark.asyncio
async def test_stream_text_deltas():
    """Basic streaming with text deltas."""
    provider = _make_provider()

    events = [
        _stream_event("response.output_text.delta", delta="Hello "),
        _stream_event("response.output_text.delta", delta="world"),
    ]
    final = _make_final_response(
        usage=SimpleNamespace(
            input_tokens=20,
            output_tokens=10,
            input_tokens_details=SimpleNamespace(cached_tokens=5),
            output_tokens_details=SimpleNamespace(reasoning_tokens=3),
        ),
    )

    provider._client = SimpleNamespace(
        responses=SimpleNamespace(
            stream=lambda **kw: _FakeResponsesStreamCtxMgr(events, final)
        )
    )

    chunks: list[str] = []
    text, tcs, usage = await provider._stream({"model": "gpt-5"}, chunks.append)

    assert chunks == ["Hello ", "world"]
    assert text == "Hello world"
    assert tcs == []
    assert usage.input_tokens == 15  # 20 - 5 cached
    assert usage.output_tokens == 10
    assert usage.cache_read_tokens == 5
    assert usage.reasoning_tokens == 3


@pytest.mark.asyncio
async def test_stream_reasoning_deltas_routed_to_thinking_hooks():
    """v2.0.9: reasoning deltas go to on_thinking_start / on_thinking_end
    (buffered, flushed on block close) — they never leak into on_text_chunk.
    """
    provider = _make_provider()

    events = [
        _stream_event("response.reasoning_text.delta", delta="think"),
        _stream_event("response.reasoning_summary_text.delta", delta="sum"),
        _stream_event(
            "response.output_item.done",
            item=SimpleNamespace(type="reasoning", id="rs_1", summary=[]),
        ),
        _stream_event("response.output_text.delta", delta="ok"),
    ]
    final = _make_final_response()

    provider._client = SimpleNamespace(
        responses=SimpleNamespace(
            stream=lambda **kw: _FakeResponsesStreamCtxMgr(events, final)
        )
    )

    chunks: list[str] = []
    thinking_starts: list[None] = []
    thinking_bodies: list[str] = []
    text, tcs, usage = await provider._stream(
        {"model": "gpt-5"},
        chunks.append,
        on_thinking_start=lambda: thinking_starts.append(None),
        on_thinking_end=thinking_bodies.append,
    )

    # assistant text channel stays clean
    assert chunks == ["ok"]
    assert text == "ok"
    # thinking lifecycle landed on the dedicated hooks (buffered then
    # flushed as a single body on item.done)
    assert len(thinking_starts) == 1
    assert thinking_bodies == ["thinksum"]


@pytest.mark.asyncio
async def test_stream_tool_call_accumulation():
    """Streaming with function call deltas."""
    provider = _make_provider()

    events = [
        _stream_event(
            "response.output_item.added",
            item=SimpleNamespace(type="function_call", call_id="tc-1", name="search"),
        ),
        _stream_event("response.function_call_arguments.delta", call_id="tc-1", delta='{"q":'),
        _stream_event("response.function_call_arguments.delta", call_id="tc-1", delta=' "x"}'),
        _stream_event(
            "response.output_item.done",
            item=SimpleNamespace(
                type="function_call", call_id="tc-1", arguments='{"q": "x"}'
            ),
        ),
    ]
    final = _make_final_response()

    provider._client = SimpleNamespace(
        responses=SimpleNamespace(
            stream=lambda **kw: _FakeResponsesStreamCtxMgr(events, final)
        )
    )

    chunks: list[str] = []
    text, tcs, usage = await provider._stream({"model": "gpt-5"}, chunks.append)

    assert text == ""
    assert len(tcs) == 1
    assert tcs[0].id == "tc-1"
    assert tcs[0].name == "search"
    assert tcs[0].input == {"q": "x"}


@pytest.mark.asyncio
async def test_stream_captures_reasoning_items():
    """Reasoning items from response.output_item.done are captured."""
    provider = _make_provider()

    events = [
        _stream_event(
            "response.output_item.done",
            item={
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": "thinking"}],
                "encrypted_content": "OPAQUE",
            },
        ),
        _stream_event("response.output_text.delta", delta="done"),
    ]
    final = _make_final_response()

    provider._client = SimpleNamespace(
        responses=SimpleNamespace(
            stream=lambda **kw: _FakeResponsesStreamCtxMgr(events, final)
        )
    )

    text, tcs, usage = await provider._stream({"model": "gpt-5"}, lambda x: None)

    assert text == "done"
    assert len(provider._pending_reasoning) == 1
    assert provider._pending_reasoning[0]["id"] == "rs_1"
    assert provider._pending_reasoning[0]["encrypted_content"] == "OPAQUE"


@pytest.mark.asyncio
async def test_stream_multiple_tool_calls():
    """Multiple tool calls interleaved in the stream."""
    provider = _make_provider()

    events = [
        _stream_event(
            "response.output_item.added",
            item=SimpleNamespace(type="function_call", call_id="tc-a", name="foo"),
        ),
        _stream_event("response.function_call_arguments.delta", call_id="tc-a", delta='{"a":1}'),
        _stream_event(
            "response.output_item.added",
            item=SimpleNamespace(type="function_call", call_id="tc-b", name="bar"),
        ),
        _stream_event("response.function_call_arguments.delta", call_id="tc-b", delta='{"b":2}'),
        _stream_event(
            "response.output_item.done",
            item=SimpleNamespace(type="function_call", call_id="tc-a", arguments='{"a":1}'),
        ),
        _stream_event(
            "response.output_item.done",
            item=SimpleNamespace(type="function_call", call_id="tc-b", arguments='{"b":2}'),
        ),
    ]
    final = _make_final_response()

    provider._client = SimpleNamespace(
        responses=SimpleNamespace(
            stream=lambda **kw: _FakeResponsesStreamCtxMgr(events, final)
        )
    )

    text, tcs, usage = await provider._stream({"model": "gpt-5"}, lambda x: None)

    assert len(tcs) == 2
    names = {tc.name for tc in tcs}
    assert names == {"foo", "bar"}


@pytest.mark.asyncio
async def test_stream_ignores_unknown_event_types():
    """Unknown event types are harmlessly skipped."""
    provider = _make_provider()

    events = [
        _stream_event("response.custom.unknown", delta="ignored"),
        _stream_event("response.output_text.delta", delta="hi"),
    ]
    final = _make_final_response()

    provider._client = SimpleNamespace(
        responses=SimpleNamespace(
            stream=lambda **kw: _FakeResponsesStreamCtxMgr(events, final)
        )
    )

    text, tcs, usage = await provider._stream({"model": "gpt-5"}, lambda x: None)
    assert text == "hi"


@pytest.mark.asyncio
async def test_stream_usage_extracted_from_final_response():
    """Usage is taken from get_final_response(), not from stream events."""
    provider = _make_provider()

    events = []
    final = _make_final_response(
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            input_tokens_details=SimpleNamespace(cached_tokens=20),
            output_tokens_details=SimpleNamespace(reasoning_tokens=10),
        ),
    )

    provider._client = SimpleNamespace(
        responses=SimpleNamespace(
            stream=lambda **kw: _FakeResponsesStreamCtxMgr(events, final)
        )
    )

    text, tcs, usage = await provider._stream({"model": "gpt-5"}, lambda x: None)

    assert usage.input_tokens == 80  # 100 - 20 cached
    assert usage.output_tokens == 50
    assert usage.cache_read_tokens == 20
    assert usage.reasoning_tokens == 10


# ======================================================================
# 6. Additional bugs found during comparison
# ======================================================================


def test_convert_tool_result_preserves_string_content():
    """Post-fix (Bug 20): a Message(role='tool', content='plain string') is
    now converted into a `function_call_output` item instead of being silently
    dropped."""
    msg = Message(role="tool", content="plain string result")
    items = _convert_tool_result(msg)
    assert len(items) == 1
    assert items[0]["type"] == "function_call_output"
    assert "plain string result" in items[0]["output"]


# ======================================================================
# 7. Chunk-idle watchdog
# ======================================================================


class _HangingStreamCtxMgr:
    """Emits one event promptly, then hangs forever — simulates a stalled
    upstream socket that never closes."""

    def __init__(self, first_event: Any, final_response: Any) -> None:
        self._first = first_event
        self._final = final_response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __aiter__(self):
        async def _gen():
            yield self._first
            import asyncio as _asyncio
            await _asyncio.sleep(3600)
            # unreached
            return

        return _gen()

    async def get_final_response(self):
        return self._final


@pytest.mark.asyncio
async def test_stream_aborts_on_event_idle_timeout(monkeypatch):
    """If the OpenAI SDK stream yields one event then stalls, the watchdog
    must raise ProviderTimeoutError well before any read-level timeout."""
    from butterfly.llm_engine.errors import ProviderTimeoutError
    from butterfly.llm_engine.providers import openai_responses as or_mod

    monkeypatch.setattr(or_mod, "_CHUNK_IDLE_TIMEOUT", 0.05)

    provider = _make_provider()
    first = _stream_event("response.output_text.delta", delta="hi")
    final = _make_final_response()

    provider._client = SimpleNamespace(
        responses=SimpleNamespace(
            stream=lambda **kw: _HangingStreamCtxMgr(first, final)
        )
    )

    with pytest.raises(ProviderTimeoutError) as exc:
        await provider._stream({"model": "gpt-5"}, lambda _: None)
    assert "stalled" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_stream_normal_flow_does_not_trip_watchdog(monkeypatch):
    """Back-to-back events that finish promptly must not raise even with a
    very tight idle timeout."""
    from butterfly.llm_engine.providers import openai_responses as or_mod

    monkeypatch.setattr(or_mod, "_CHUNK_IDLE_TIMEOUT", 0.5)

    provider = _make_provider()
    events = [
        _stream_event("response.output_text.delta", delta="a"),
        _stream_event("response.output_text.delta", delta="b"),
    ]
    final = _make_final_response()

    provider._client = SimpleNamespace(
        responses=SimpleNamespace(
            stream=lambda **kw: _FakeResponsesStreamCtxMgr(events, final)
        )
    )

    text, _, _ = await provider._stream({"model": "gpt-5"}, lambda _: None)
    assert text == "ab"