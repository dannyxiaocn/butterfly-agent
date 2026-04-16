"""Unit tests for CodexProvider — request body, error taxonomy, reasoning replay.

These tests don't hit the network; they exercise the pure helpers directly.
"""
from __future__ import annotations

import json

import pytest

from butterfly.core.types import Message, TokenUsage
from butterfly.llm_engine.errors import (
    AuthError,
    BadRequestError,
    ContextWindowExceededError,
    ProviderError,
    RateLimitError,
    ServerError,
)
from butterfly.llm_engine.providers.codex import (
    CodexProvider,
    _build_headers,
    _build_request_body,
    _convert_assistant,
    _convert_messages,
    _convert_tool_result,
    _extract_usage,
    _is_codex_compatible_model,
    _parse_retry_after,
    _raise_from_status,
    _raise_stream_error,
    _tool_to_responses_api,
)


def test_default_model_is_gpt5():
    # ChatGPT-OAuth backend rejects "gpt-5-codex" with 400 even though
    # codex-rs defaults to it; we keep "gpt-5.4" until the backend supports
    # the codex model IDs.
    assert CodexProvider.DEFAULT_MODEL == "gpt-5.4"


def test_build_request_body_thinking_includes_encrypted_content():
    body = _build_request_body(
        "gpt-5-codex",
        "sys",
        [Message(role="user", content="hi")],
        [],
        thinking=True,
        thinking_effort="high",
        prompt_cache_key="conv-123",
    )
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["prompt_cache_key"] == "conv-123"
    assert body["stream"] is True
    assert body["store"] is False


def test_build_request_body_no_thinking_omits_reasoning_and_include():
    body = _build_request_body(
        "gpt-5-codex", "sys", [Message(role="user", content="hi")], [],
        thinking=False, prompt_cache_key="c",
    )
    assert "reasoning" not in body
    assert "include" not in body


def test_tool_schema_uses_strict_false_not_none():
    class _T:
        def to_api_dict(self):
            return {"name": "echo", "description": "Echo", "input_schema": {"type": "object"}}

    schema = _tool_to_responses_api(_T())
    assert schema["strict"] is False
    assert schema["type"] == "function"
    assert schema["name"] == "echo"


def test_headers_use_codex_cli_originator_and_session_id():
    headers = _build_headers("tok", "acct-1", "conv-uuid")
    assert headers["originator"] == "codex_cli_rs"
    assert headers["ChatGPT-Account-ID"] == "acct-1"
    assert headers["session_id"] == "conv-uuid"
    assert headers["Authorization"] == "Bearer tok"


# ── reasoning replay ─────────────────────────────────────────────────


def test_convert_assistant_replays_reasoning_block_verbatim():
    msg = Message(
        role="assistant",
        content=[
            {
                "type": "reasoning",
                "id": "rs_abc",
                "summary": [{"type": "summary_text", "text": "thinking..."}],
                "encrypted_content": "OPAQUE",
            },
            {"type": "text", "text": "final answer"},
            {"type": "tool_use", "id": "tc-1", "name": "bash", "input": {"cmd": "ls"}},
        ],
    )
    items = _convert_assistant(msg)
    # Order must be: reasoning → text message → function_call
    assert items[0]["type"] == "reasoning"
    assert items[0]["id"] == "rs_abc"
    assert items[0]["encrypted_content"] == "OPAQUE"
    assert items[1]["type"] == "message"
    assert items[2]["type"] == "function_call"
    assert items[2]["call_id"] == "tc-1"


def test_convert_messages_round_trip_user_assistant_tool():
    messages = [
        Message(role="user", content="hello"),
        Message(
            role="assistant",
            content=[
                {"type": "tool_use", "id": "t1", "name": "echo", "input": {"x": 1}},
            ],
        ),
        Message(
            role="tool",
            content=[{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        ),
    ]
    items = _convert_messages(messages)
    kinds = [i.get("type") or i.get("role") for i in items]
    assert kinds == ["user", "function_call", "function_call_output"]


# ── usage extraction ────────────────────────────────────────────────


def test_extract_usage_includes_reasoning_and_cached():
    u = _extract_usage({
        "input_tokens": 100,
        "output_tokens": 200,
        "input_tokens_details": {"cached_tokens": 30},
        "output_tokens_details": {"reasoning_tokens": 120},
    })
    assert u.input_tokens == 70  # 100 - 30 cached
    assert u.output_tokens == 200
    assert u.cache_read_tokens == 30
    assert u.reasoning_tokens == 120


def test_extract_usage_handles_missing_details():
    u = _extract_usage({"input_tokens": 10, "output_tokens": 5})
    assert u == TokenUsage(input_tokens=10, output_tokens=5)


# ── error mapping ───────────────────────────────────────────────────


def test_raise_from_status_maps_401_to_auth_error():
    with pytest.raises(AuthError):
        _raise_from_status(401, "unauthorized")


def test_raise_from_status_maps_400_to_bad_request():
    with pytest.raises(BadRequestError):
        _raise_from_status(400, "bad req")


def test_raise_from_status_maps_429_with_retry_after():
    with pytest.raises(RateLimitError) as exc:
        _raise_from_status(429, "try again in 5s")
    assert exc.value.retry_after == 5.0


def test_raise_from_status_maps_500_to_server_error():
    with pytest.raises(ServerError):
        _raise_from_status(503, "overloaded")


def test_raise_from_status_unknown_raises_generic_provider_error():
    with pytest.raises(ProviderError):
        _raise_from_status(418, "teapot")


def test_raise_stream_error_context_length():
    event = {"type": "response.failed", "response": {"error": {"code": "context_length_exceeded", "message": "too long"}}}
    with pytest.raises(ContextWindowExceededError):
        _raise_stream_error(event)


def test_raise_stream_error_rate_limit():
    event = {"type": "response.failed", "response": {"error": {"code": "rate_limit_exceeded", "message": "retry in 2 minutes"}}}
    with pytest.raises(RateLimitError) as exc:
        _raise_stream_error(event)
    assert exc.value.retry_after == 120.0


def test_raise_stream_error_generic_provider_error():
    event = {"type": "error", "message": "something broke"}
    with pytest.raises(ProviderError):
        _raise_stream_error(event)


# ── retry-after parsing ─────────────────────────────────────────────


@pytest.mark.parametrize("text, expected", [
    ("retry in 5 seconds", 5.0),
    ("retry in 5s", 5.0),
    ("retry in 500ms", 0.5),
    ("wait 2 minutes", 120.0),
    ("try again in 3min", 180.0),
    ("", None),
    ("no hints here", None),
])
def test_parse_retry_after(text, expected):
    assert _parse_retry_after(text) == expected


# ── consume_extra_blocks ────────────────────────────────────────────


def test_consume_extra_blocks_drains_pending_reasoning():
    p = CodexProvider.__new__(CodexProvider)
    p._pending_reasoning = [{"type": "reasoning", "id": "rs_1"}]
    first = p.consume_extra_blocks()
    second = p.consume_extra_blocks()
    assert first == [{"type": "reasoning", "id": "rs_1"}]
    assert second == []


def test_registry_has_codex_oauth():
    from butterfly.llm_engine.registry import _REGISTRY
    assert "codex-oauth" in _REGISTRY


# ── SSE stream parser ───────────────────────────────────────────────


class _FakeSSEResponse:
    """Mimics httpx stream response — yields byte chunks via aiter_bytes()."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


def _sse_event(event_dict: dict) -> bytes:
    return f"data: {json.dumps(event_dict)}\n\n".encode()


@pytest.mark.asyncio
async def test_parse_sse_stream_text_and_tool_call():
    from butterfly.llm_engine.providers.codex import _parse_sse_stream

    chunks = [
        _sse_event({"type": "response.output_text.delta", "delta": "Hello "}),
        _sse_event({"type": "response.output_text.delta", "delta": "world"}),
        _sse_event({
            "type": "response.output_item.added",
            "item": {"type": "function_call", "call_id": "tc-42", "name": "bash"},
        }),
        _sse_event({
            "type": "response.function_call_arguments.delta",
            "call_id": "tc-42",
            "delta": '{"cmd":',
        }),
        _sse_event({
            "type": "response.function_call_arguments.delta",
            "call_id": "tc-42",
            "delta": ' "ls"}',
        }),
        _sse_event({
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "tc-42",
                "arguments": '{"cmd": "ls"}',
            },
        }),
        _sse_event({
            "type": "response.completed",
            "response": {
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 8,
                    "input_tokens_details": {"cached_tokens": 5},
                    "output_tokens_details": {"reasoning_tokens": 4},
                }
            },
        }),
    ]

    streamed: list[str] = []
    text, tool_calls, usage, reasoning_items = await _parse_sse_stream(
        _FakeSSEResponse(chunks), streamed.append
    )

    assert streamed == ["Hello ", "world"]
    assert text == "Hello world"
    assert len(tool_calls) == 1
    assert tool_calls[0].id == "tc-42"
    assert tool_calls[0].name == "bash"
    assert tool_calls[0].input == {"cmd": "ls"}
    assert usage.input_tokens == 15  # 20 - 5 cached
    assert usage.cache_read_tokens == 5
    assert usage.reasoning_tokens == 4
    assert reasoning_items == []


@pytest.mark.asyncio
async def test_parse_sse_stream_captures_reasoning_items():
    from butterfly.llm_engine.providers.codex import _parse_sse_stream

    chunks = [
        _sse_event({
            "type": "response.output_item.done",
            "item": {
                "type": "reasoning",
                "id": "rs_9",
                "summary": [{"type": "summary_text", "text": "thinking"}],
                "encrypted_content": "OPAQUE",
            },
        }),
        _sse_event({"type": "response.output_text.delta", "delta": "ok"}),
        _sse_event({"type": "response.completed", "response": {"usage": {}}}),
    ]

    text, tool_calls, usage, reasoning_items = await _parse_sse_stream(
        _FakeSSEResponse(chunks), None
    )

    assert text == "ok"
    assert tool_calls == []
    assert len(reasoning_items) == 1
    assert reasoning_items[0]["id"] == "rs_9"
    assert reasoning_items[0]["encrypted_content"] == "OPAQUE"


@pytest.mark.asyncio
async def test_parse_sse_stream_raises_on_incomplete_context_length():
    from butterfly.llm_engine.providers.codex import _parse_sse_stream

    chunks = [
        _sse_event({
            "type": "response.incomplete",
            "response": {"incomplete_details": {"reason": "context_length"}},
        }),
    ]
    with pytest.raises(ContextWindowExceededError):
        await _parse_sse_stream(_FakeSSEResponse(chunks), None)


@pytest.mark.asyncio
async def test_parse_sse_stream_ignores_malformed_json():
    from butterfly.llm_engine.providers.codex import _parse_sse_stream

    chunks = [
        b"data: not json\n\n",
        _sse_event({"type": "response.output_text.delta", "delta": "hi"}),
        _sse_event({"type": "response.completed", "response": {"usage": {}}}),
    ]
    text, _, _, _ = await _parse_sse_stream(_FakeSSEResponse(chunks), None)
    assert text == "hi"


@pytest.mark.asyncio
async def test_parse_sse_stream_handles_split_events_across_chunks():
    """An event split mid-payload across two byte chunks must still parse."""
    from butterfly.llm_engine.providers.codex import _parse_sse_stream

    payload = _sse_event({"type": "response.output_text.delta", "delta": "split"})
    completed = _sse_event({"type": "response.completed", "response": {"usage": {}}})
    mid = len(payload) // 2
    chunks = [payload[:mid], payload[mid:], completed]

    text, _, _, _ = await _parse_sse_stream(_FakeSSEResponse(chunks), None)
    assert text == "split"


# ── thinking routing: v2.0.11 regression coverage ───────────────────


@pytest.mark.asyncio
async def test_output_text_delta_inside_reasoning_item_routes_to_thinking():
    """Backend wraps reasoning summary in a reasoning output_item but streams
    body via output_text.delta — must not leak into assistant text."""
    from butterfly.llm_engine.providers.codex import _parse_sse_stream

    chunks = [
        _sse_event({
            "type": "response.output_item.added",
            "item": {"type": "reasoning", "id": "rs_1"},
        }),
        _sse_event({"type": "response.output_text.delta", "delta": "**Plan**\n\n"}),
        _sse_event({"type": "response.output_text.delta", "delta": "do X then Y"}),
        _sse_event({
            "type": "response.output_item.done",
            "item": {"type": "reasoning", "id": "rs_1", "summary": [
                {"type": "summary_text", "text": "**Plan**\n\ndo X then Y"},
            ]},
        }),
        _sse_event({
            "type": "response.output_item.added",
            "item": {"type": "message"},
        }),
        _sse_event({"type": "response.output_text.delta", "delta": "Done."}),
        _sse_event({
            "type": "response.output_item.done",
            "item": {"type": "message"},
        }),
        _sse_event({"type": "response.completed", "response": {"usage": {}}}),
    ]
    text_chunks: list[str] = []
    thinking_bodies: list[str] = []
    started: list[bool] = []
    text, _, _, reasoning_items = await _parse_sse_stream(
        _FakeSSEResponse(chunks),
        text_chunks.append,
        on_thinking_start=lambda: started.append(True),
        on_thinking_end=thinking_bodies.append,
    )
    assert text == "Done."
    assert text_chunks == ["Done."]
    assert thinking_bodies == ["**Plan**\n\ndo X then Y"]
    assert started == [True]
    assert len(reasoning_items) == 1


@pytest.mark.asyncio
async def test_reasoning_item_done_falls_back_to_summary_text():
    """When no streaming deltas arrive (encrypted-only path or unknown
    delta etype), thinking body is extracted from item.summary."""
    from butterfly.llm_engine.providers.codex import _parse_sse_stream

    chunks = [
        _sse_event({
            "type": "response.output_item.done",
            "item": {
                "type": "reasoning",
                "id": "rs_2",
                "summary": [
                    {"type": "summary_text", "text": "first thought"},
                    {"type": "summary_text", "text": "second thought"},
                ],
                "encrypted_content": "OPAQUE",
            },
        }),
        _sse_event({"type": "response.completed", "response": {"usage": {}}}),
    ]
    bodies: list[str] = []
    await _parse_sse_stream(
        _FakeSSEResponse(chunks),
        None,
        on_thinking_start=lambda: None,
        on_thinking_end=bodies.append,
    )
    assert bodies == ["first thought\n\nsecond thought"]


@pytest.mark.asyncio
async def test_unknown_reasoning_etype_routes_to_thinking():
    """Catch-all: any response.reasoning* event variant routes to thinking."""
    from butterfly.llm_engine.providers.codex import _parse_sse_stream

    chunks = [
        _sse_event({"type": "response.reasoning_summary.delta", "delta": "hmm"}),
        _sse_event({"type": "response.reasoning_summary_part.done",
                    "part": {"text": " more"}}),
        _sse_event({"type": "response.completed", "response": {"usage": {}}}),
    ]
    bodies: list[str] = []
    text, _, _, _ = await _parse_sse_stream(
        _FakeSSEResponse(chunks),
        None,
        on_thinking_start=lambda: None,
        on_thinking_end=bodies.append,
    )
    assert text == ""
    assert bodies == ["hmm more"]


# ── request body: prompt_cache_key absence ──────────────────────────


def test_build_request_body_omits_prompt_cache_key_when_empty():
    body = _build_request_body(
        "gpt-5-codex", "sys", [Message(role="user", content="hi")], [],
        thinking=False, prompt_cache_key="",
    )
    assert "prompt_cache_key" not in body


def test_build_request_body_serializes_tools():
    class _T:
        def to_api_dict(self):
            return {"name": "bash", "description": "Run", "input_schema": {"type": "object"}}

    body = _build_request_body(
        "gpt-5-codex", "sys", [Message(role="user", content="hi")], [_T()],
        thinking=False, prompt_cache_key="c",
    )
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["name"] == "bash"
    assert body["tools"][0]["strict"] is False


# ── assistant message with no encrypted_content on reasoning ────────


def test_convert_assistant_reasoning_without_encrypted_content():
    """If the server didn't return encrypted_content, replay must still be valid (no crash)."""
    msg = Message(
        role="assistant",
        content=[
            {"type": "reasoning", "id": "rs_x", "summary": []},
            {"type": "text", "text": "t"},
        ],
    )
    items = _convert_assistant(msg)
    assert items[0]["type"] == "reasoning"
    assert "encrypted_content" not in items[0]
    assert items[0]["id"] == "rs_x"


# ── BUG-7 regression: model-substitution no longer couples to claude-sonnet-4-6 ──


@pytest.mark.parametrize("model, expected", [
    ("gpt-5.4", True),
    ("gpt-5-codex", True),
    ("o3-mini", True),
    ("", False),
    (None, False),
    ("claude-sonnet-4-6", False),
    ("claude-opus-4-6", False),
    ("claude-haiku-4-5", False),
])
def test_is_codex_compatible_model(model, expected):
    assert _is_codex_compatible_model(model) is expected


# ── BUG-8 regression: multi-text tool_result concatenation ──


def test_convert_tool_result_multi_text_concatenated_without_space():
    msg = Message(
        role="tool",
        content=[{
            "type": "tool_result",
            "tool_use_id": "tc-x",
            "content": [{"type": "text", "text": "foo"}, {"type": "text", "text": "bar"}],
        }],
    )
    items = _convert_tool_result(msg)
    # Byte-for-byte concatenation — no stray space (BUG-8).
    assert items[0]["output"] == "foobar"


# ── BUG-6 regression: invalid thinking_effort falls back to "medium" ──


@pytest.mark.asyncio
async def test_complete_invalid_effort_sends_medium_in_body(monkeypatch):
    """An invalid thinking_effort must produce reasoning.effort=medium on the wire.

    Addresses the cubic P3 comment on PR #20: the earlier test only asserted
    set membership, which didn't actually exercise the clamp. This intercepts
    the real httpx request body and inspects reasoning.effort.
    """
    import httpx

    from butterfly.llm_engine.providers import codex as codex_mod
    from butterfly.core.types import Message

    provider = CodexProvider.__new__(CodexProvider)
    provider.max_tokens = 100
    provider._conversation_id = "test-conv"
    provider._pending_reasoning = []

    async def fake_get_auth_async(self, *, force_refresh=False, rejected_token=""):
        return "token", "acct-1"

    provider._get_auth_async = fake_get_auth_async.__get__(provider, CodexProvider)

    captured_body: dict = {}

    class _FakeResponse:
        status_code = 200
        async def aread(self):
            return b""
        async def aiter_bytes(self):
            # Emit a single response.completed event so _parse_sse_stream terminates cleanly.
            yield b'data: {"type":"response.completed","response":{"usage":{}}}\n\n'

    class _FakeStreamCtx:
        def __init__(self, body):
            captured_body.update(body)
        async def __aenter__(self):
            return _FakeResponse()
        async def __aexit__(self, *args):
            return False

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def stream(self, method, url, *, headers, json, **kw):
            return _FakeStreamCtx(json)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="gpt-5.4",
        thinking=True,
        thinking_effort="bogus",  # invalid — must clamp to "medium"
    )

    assert captured_body.get("reasoning", {}).get("effort") == "medium"
