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
    _read_auth,
    _tool_to_responses_api,
    _write_auth,
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
    p._pending_builtin_items = []
    p._pending_builtin_progress = []
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
    text, tool_calls, usage, reasoning_items, _bi, _bp = await _parse_sse_stream(
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

    text, tool_calls, usage, reasoning_items, _bi, _bp = await _parse_sse_stream(
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
    text, *_ = await _parse_sse_stream(_FakeSSEResponse(chunks), None)
    assert text == "hi"


@pytest.mark.asyncio
async def test_parse_sse_stream_handles_split_events_across_chunks():
    """An event split mid-payload across two byte chunks must still parse."""
    from butterfly.llm_engine.providers.codex import _parse_sse_stream

    payload = _sse_event({"type": "response.output_text.delta", "delta": "split"})
    completed = _sse_event({"type": "response.completed", "response": {"usage": {}}})
    mid = len(payload) // 2
    chunks = [payload[:mid], payload[mid:], completed]

    text, *_ = await _parse_sse_stream(_FakeSSEResponse(chunks), None)
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
    text, _, _, reasoning_items, _bi, _bp = await _parse_sse_stream(
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
    """Catch-all: an unrecognised ``response.reasoning*`` ``.delta`` event
    variant still routes its delta text to thinking (forward-compat for
    backend event-name drift). Non-delta variants (``.done`` / ``.part.*``)
    carry cumulative text that the delta stream already delivered, so
    they are silently consumed — see
    ``test_repeated_reasoning_done_events_do_not_triple_body``."""
    from butterfly.llm_engine.providers.codex import _parse_sse_stream

    chunks = [
        _sse_event({"type": "response.reasoning_summary.delta", "delta": "hmm"}),
        _sse_event({"type": "response.reasoning_summary.delta", "delta": " more"}),
        _sse_event({"type": "response.completed", "response": {"usage": {}}}),
    ]
    bodies: list[str] = []
    text, *_ = await _parse_sse_stream(
        _FakeSSEResponse(chunks),
        None,
        on_thinking_start=lambda: None,
        on_thinking_end=bodies.append,
    )
    assert text == ""
    assert bodies == ["hmm more"]


@pytest.mark.asyncio
async def test_repeated_reasoning_done_events_do_not_triple_body():
    """Regression for the codex-oauth ``gpt-5.4`` triple-render bug.

    The real Codex SSE stream for a reasoning item emits the summary body
    across three channels:

      1. ``response.reasoning_summary_text.delta`` × N — the text, piece by
         piece (explicitly handled).
      2. ``response.reasoning_summary_text.done`` — the full cumulative
         body (matched by the ``response.reasoning*`` catch-all).
      3. ``response.reasoning_summary_part.done`` — the full part body
         again (also matched by the catch-all).

    Before the fix, the catch-all appended the ``.done`` text to
    ``thinking_parts`` on top of what the deltas already streamed, so the
    final body rendered the thought **three times** inside the cell. The
    fix narrows the catch-all to ``.delta`` variants only.
    """
    from butterfly.llm_engine.providers.codex import _parse_sse_stream

    body = "**Calculating**\n\nThe answer is 1255."

    chunks = [
        _sse_event({
            "type": "response.output_item.added",
            "item": {"type": "reasoning", "id": "rs_1"},
        }),
        _sse_event({
            "type": "response.reasoning_summary_part.added",
            "part": {"type": "summary_text", "text": ""},
        }),
        _sse_event({"type": "response.reasoning_summary_text.delta", "delta": "**Calculating**\n\n"}),
        _sse_event({"type": "response.reasoning_summary_text.delta", "delta": "The answer is 1255."}),
        _sse_event({"type": "response.reasoning_summary_text.done", "text": body}),
        _sse_event({
            "type": "response.reasoning_summary_part.done",
            "part": {"type": "summary_text", "text": body},
        }),
        _sse_event({
            "type": "response.output_item.done",
            "item": {"type": "reasoning", "id": "rs_1", "summary": [
                {"type": "summary_text", "text": body},
            ]},
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

    # Exactly one thinking_end fires (one reasoning item = one cell).
    assert len(bodies) == 1
    assert bodies[0] == body
    # The body must NOT contain the text repeated. The pre-fix code
    # produced body * 3; pin it at exactly one occurrence.
    assert bodies[0].count("The answer is 1255.") == 1


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


# ── Fix 4: warn-log when unsupported thinking_effort clamps to medium ──


@pytest.mark.asyncio
async def test_complete_unsupported_effort_logs_warning(monkeypatch, caplog):
    """A non-empty unsupported ``thinking_effort`` (notably the Anthropic-only
    ``"max"``) must log a warning before clamping to ``"medium"`` so the
    silent YAML-misconfiguration case is visible in logs.
    """
    import logging as _logging
    import httpx

    from butterfly.core.types import Message

    provider = CodexProvider.__new__(CodexProvider)
    provider.max_tokens = 100
    provider._conversation_id = "test-conv-warn"
    provider._pending_reasoning = []
    provider._pending_builtin_items = []
    provider._pending_builtin_progress = []

    async def fake_get_auth_async(self, *, force_refresh=False, rejected_token=""):
        return "token", "acct-1"

    provider._get_auth_async = fake_get_auth_async.__get__(provider, CodexProvider)

    captured_body: dict = {}

    class _FakeResponse:
        status_code = 200
        async def aread(self):
            return b""
        async def aiter_bytes(self):
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

    with caplog.at_level(_logging.WARNING, logger="butterfly.llm_engine.providers.codex"):
        await provider.complete(
            messages=[Message(role="user", content="hi")],
            tools=[],
            system_prompt="sys",
            model="gpt-5.4",
            thinking=True,
            thinking_effort="max",  # Anthropic-Opus-only — Codex must clamp + warn
        )

    # Clamped on the wire.
    assert captured_body.get("reasoning", {}).get("effort") == "medium"
    # Warning logged with the offending value.
    warnings = [r for r in caplog.records if r.levelno == _logging.WARNING]
    assert any("max" in r.getMessage() and "thinking_effort" in r.getMessage() for r in warnings), (
        f"expected a warning mentioning 'max' and 'thinking_effort'; got: "
        f"{[r.getMessage() for r in warnings]}"
    )


@pytest.mark.asyncio
async def test_complete_empty_effort_does_not_warn(monkeypatch, caplog):
    """An empty / None ``thinking_effort`` (the default path when thinking is
    off or the caller didn't pass one) must NOT log a warning — logs would be
    noisy for the common case.
    """
    import logging as _logging
    import httpx

    from butterfly.core.types import Message

    provider = CodexProvider.__new__(CodexProvider)
    provider.max_tokens = 100
    provider._conversation_id = "test-conv-empty"
    provider._pending_reasoning = []
    provider._pending_builtin_items = []
    provider._pending_builtin_progress = []

    async def fake_get_auth_async(self, *, force_refresh=False, rejected_token=""):
        return "token", "acct-1"

    provider._get_auth_async = fake_get_auth_async.__get__(provider, CodexProvider)

    class _FakeResponse:
        status_code = 200
        async def aread(self):
            return b""
        async def aiter_bytes(self):
            yield b'data: {"type":"response.completed","response":{"usage":{}}}\n\n'

    class _FakeStreamCtx:
        def __init__(self, body):
            pass
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

    with caplog.at_level(_logging.WARNING, logger="butterfly.llm_engine.providers.codex"):
        await provider.complete(
            messages=[Message(role="user", content="hi")],
            tools=[],
            system_prompt="sys",
            model="gpt-5.4",
            thinking=True,
            thinking_effort="",  # empty → silent clamp
        )

    warnings = [r for r in caplog.records if r.levelno == _logging.WARNING]
    # Empty string is falsy → no warning expected.
    assert not any("thinking_effort" in r.getMessage() for r in warnings)


# ── _read_auth migration (butterfly-owned auth store) ─────────────────────────


def test_read_auth_uses_butterfly_path_when_present(monkeypatch, tmp_path):
    """When ~/.butterfly/auth.json exists, _read_auth() reads it directly."""
    import json as _json
    from butterfly.llm_engine.providers import codex as codex_mod

    butterfly_auth = tmp_path / "butterfly_auth.json"
    data = {"tokens": {"access_token": "butterfly-tok", "refresh_token": "r"}}
    butterfly_auth.write_text(_json.dumps(data))

    monkeypatch.setattr(codex_mod, "_AUTH_PATH", butterfly_auth)
    # CLI auth path exists too but should be ignored when butterfly's file is present.
    cli_auth = tmp_path / "codex_auth.json"
    cli_auth.write_text(_json.dumps({"tokens": {"access_token": "cli-tok", "refresh_token": "r"}}))
    monkeypatch.setattr(codex_mod, "_CODEX_CLI_AUTH_PATH", cli_auth)

    result = _read_auth()
    assert result["tokens"]["access_token"] == "butterfly-tok"


def test_read_auth_migrates_from_codex_cli_on_first_use(monkeypatch, tmp_path):
    """When ~/.butterfly/auth.json is absent but ~/.codex/auth.json exists,
    _read_auth() migrates the tokens and writes ~/.butterfly/auth.json."""
    import json as _json
    from butterfly.llm_engine.providers import codex as codex_mod

    butterfly_auth = tmp_path / "butterfly_auth.json"  # does NOT exist yet
    cli_auth = tmp_path / "codex_auth.json"
    cli_data = {"tokens": {"access_token": "cli-tok", "refresh_token": "cli-r"}}
    cli_auth.write_text(_json.dumps(cli_data))

    monkeypatch.setattr(codex_mod, "_AUTH_PATH", butterfly_auth)
    monkeypatch.setattr(codex_mod, "_CODEX_CLI_AUTH_PATH", cli_auth)

    result = _read_auth()
    assert result["tokens"]["access_token"] == "cli-tok"
    # Migration must have written the butterfly auth file.
    assert butterfly_auth.exists(), "_read_auth() must write the butterfly auth file on migration"
    stored = _json.loads(butterfly_auth.read_text())
    assert stored["tokens"]["access_token"] == "cli-tok"


def test_read_auth_raises_when_neither_file_exists(monkeypatch, tmp_path):
    """When both auth files are absent, _read_auth() raises AuthError."""
    from butterfly.llm_engine.providers import codex as codex_mod
    from butterfly.llm_engine.errors import AuthError

    monkeypatch.setattr(codex_mod, "_AUTH_PATH", tmp_path / "nope_butterfly.json")
    monkeypatch.setattr(codex_mod, "_CODEX_CLI_AUTH_PATH", tmp_path / "nope_codex.json")

    with pytest.raises(AuthError):
        _read_auth()


def test_read_auth_migration_skipped_when_cli_tokens_malformed(monkeypatch, tmp_path):
    """Malformed ~/.codex/auth.json should not cause a migration; AuthError is raised."""
    import json as _json
    from butterfly.llm_engine.providers import codex as codex_mod
    from butterfly.llm_engine.errors import AuthError

    butterfly_auth = tmp_path / "butterfly_auth.json"
    cli_auth = tmp_path / "codex_auth.json"
    cli_auth.write_text("{{ not valid json")

    monkeypatch.setattr(codex_mod, "_AUTH_PATH", butterfly_auth)
    monkeypatch.setattr(codex_mod, "_CODEX_CLI_AUTH_PATH", cli_auth)

    with pytest.raises(AuthError):
        _read_auth()


# ── refresh: account_id re-extraction + persistence ────────────────────────
#
# The backend server may rotate ``chatgpt_account_id`` as part of a refresh
# (rare; opencode defends against it explicitly). If we kept using the
# ``account_id`` field persisted from a previous session, the retried request
# would 401 again. ``_refresh_access_token_async`` must re-extract from the
# fresh JWT; ``_get_auth_async`` must prefer the persisted (post-refresh)
# value so both paths converge.


def _make_jwt_with_account(account_id: str) -> str:
    """Build a minimal unsigned JWT carrying chatgpt_account_id in the auth claim."""
    import base64 as _b64
    import json as _json
    from butterfly.llm_engine.providers.codex import _JWT_AUTH_CLAIM

    payload = {_JWT_AUTH_CLAIM: {"chatgpt_account_id": account_id}, "exp": 9999999999}
    header_b64 = _b64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload_b64 = (
        _b64.urlsafe_b64encode(_json.dumps(payload).encode()).rstrip(b"=").decode()
    )
    return f"{header_b64}.{payload_b64}.sig"


@pytest.mark.asyncio
async def test_refresh_reextracts_and_persists_fresh_account_id(monkeypatch, tmp_path):
    """After a refresh, the fresh access_token's account_id must win over any
    stale ``account_id`` field on disk AND be re-persisted."""
    import json as _json
    from butterfly.llm_engine.providers import codex as codex_mod

    fresh_access = _make_jwt_with_account("fresh-456")
    auth_path = tmp_path / "auth.json"
    stale = {
        "tokens": {
            "access_token": "expired-token",  # _is_token_expired returns True (unparseable)
            "refresh_token": "r",
            "id_token": "",
            "account_id": "stale-123",  # lingering from a previous session
        }
    }
    auth_path.write_text(_json.dumps(stale))
    monkeypatch.setattr(codex_mod, "_AUTH_PATH", auth_path)

    async def fake_refresh(refresh_token):
        assert refresh_token == "r"
        return {
            "access_token": fresh_access,
            "refresh_token": "r2",
            "id_token": "",
            "account_id": codex_mod._extract_account_id(fresh_access, ""),
        }

    monkeypatch.setattr(codex_mod, "_refresh_access_token_async", fake_refresh)

    provider = CodexProvider.__new__(CodexProvider)
    provider._conversation_id = "c"
    provider._pending_reasoning = []

    access, account_id = await provider._get_auth_async()
    assert access == fresh_access
    assert account_id == "fresh-456"

    # The disk file was rewritten — the stale account_id must be gone.
    rewritten = _json.loads(auth_path.read_text())
    assert rewritten["tokens"]["account_id"] == "fresh-456"


@pytest.mark.asyncio
async def test_get_auth_prefers_persisted_account_id_when_present(monkeypatch, tmp_path):
    """No refresh path: when the access_token is still valid, we trust the
    already-persisted ``account_id`` (written by the last refresh) instead of
    re-extracting on every call."""
    import json as _json
    from butterfly.llm_engine.providers import codex as codex_mod

    # Unexpired token (exp way in the future) whose JWT account is ``jwt-only``
    token = _make_jwt_with_account("jwt-only")
    auth_path = tmp_path / "auth.json"
    data = {
        "tokens": {
            "access_token": token,
            "refresh_token": "r",
            "id_token": "",
            "account_id": "persisted-999",
        }
    }
    auth_path.write_text(_json.dumps(data))
    monkeypatch.setattr(codex_mod, "_AUTH_PATH", auth_path)

    provider = CodexProvider.__new__(CodexProvider)
    provider._conversation_id = "c"
    provider._pending_reasoning = []

    _, account_id = await provider._get_auth_async()
    assert account_id == "persisted-999"


@pytest.mark.asyncio
async def test_get_auth_falls_back_to_jwt_extraction_when_no_persisted_account(
    monkeypatch, tmp_path
):
    """Legacy auth.json lacking ``account_id`` (e.g. pre-v2.0.x or freshly
    CLI-migrated) must still work — falls back to JWT extraction."""
    import json as _json
    from butterfly.llm_engine.providers import codex as codex_mod

    token = _make_jwt_with_account("from-jwt-42")
    auth_path = tmp_path / "auth.json"
    data = {
        "tokens": {
            "access_token": token,
            "refresh_token": "r",
            "id_token": "",
            # NOTE: no account_id key
        }
    }
    auth_path.write_text(_json.dumps(data))
    monkeypatch.setattr(codex_mod, "_AUTH_PATH", auth_path)

    provider = CodexProvider.__new__(CodexProvider)
    provider._conversation_id = "c"
    provider._pending_reasoning = []

    _, account_id = await provider._get_auth_async()
    assert account_id == "from-jwt-42"


# ── chunk-idle watchdog ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_sse_stream_aborts_on_chunk_idle_timeout(monkeypatch):
    """If the upstream connection hangs mid-stream (one chunk then silence),
    the watchdog must raise ProviderTimeoutError rather than blocking for the
    full read timeout."""
    import asyncio as _asyncio
    from butterfly.llm_engine.errors import ProviderTimeoutError
    from butterfly.llm_engine.providers import codex as codex_mod
    from butterfly.llm_engine.providers.codex import _parse_sse_stream

    monkeypatch.setattr(codex_mod, "_CHUNK_IDLE_TIMEOUT", 0.05)

    class _HangingResponse:
        async def aiter_bytes(self):
            # First chunk arrives promptly, then the connection stalls.
            yield _sse_event({"type": "response.output_text.delta", "delta": "hi"})
            # Block forever — simulates a stalled socket with no close.
            await _asyncio.sleep(3600)
            # Unreached — kept to document the intent.
            yield b""

    with pytest.raises(ProviderTimeoutError) as exc:
        await _parse_sse_stream(_HangingResponse(), None)
    assert "stalled" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_parse_sse_stream_normal_stream_does_not_trigger_watchdog(monkeypatch):
    """A well-behaved stream that finishes promptly must not raise."""
    from butterfly.llm_engine.providers import codex as codex_mod
    from butterfly.llm_engine.providers.codex import _parse_sse_stream

    # Even at a very tight timeout, back-to-back chunks must sail through.
    monkeypatch.setattr(codex_mod, "_CHUNK_IDLE_TIMEOUT", 0.5)

    chunks = [
        _sse_event({"type": "response.output_text.delta", "delta": "ok"}),
        _sse_event({"type": "response.completed", "response": {"usage": {}}}),
    ]
    text, *_ = await _parse_sse_stream(_FakeSSEResponse(chunks), None)
    assert text == "ok"
