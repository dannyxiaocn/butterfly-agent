"""Built-in-tool tests for :class:`OpenAIResponsesProvider`.

Mirrors :mod:`test_codex_builtin_tools` but exercises the SDK-event path
(``client.responses.stream``) instead of raw SSE bytes.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from butterfly.core.tool import Tool
from butterfly.core.types import Message
from butterfly.llm_engine.providers.openai_responses import (
    OpenAIResponsesProvider,
    _classify_builtin_event,
    _convert_assistant,
    _format_tool_for_request,
)


# ======================================================================
# Fake stream infra (same pattern as test_openai_responses_provider_new.py)
# ======================================================================


class _FakeStream:
    def __init__(self, events: list[Any], final: Any) -> None:
        self._events = events
        self._final = final

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
        return self._final


def _make_provider() -> OpenAIResponsesProvider:
    p = OpenAIResponsesProvider.__new__(OpenAIResponsesProvider)
    p.max_tokens = 8096
    p._conversation_id = "conv-1"
    p._pending_reasoning = []
    p._pending_builtin_items = []
    p._pending_builtin_progress = []
    p._client = None
    return p


def _evt(etype: str, **fields) -> SimpleNamespace:
    return SimpleNamespace(type=etype, **fields)


def _final(output: list[Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        output=output or [],
        usage=SimpleNamespace(
            input_tokens=0,
            output_tokens=0,
            input_tokens_details=None,
            output_tokens_details=None,
        ),
    )


# ======================================================================
# Classifier — re-covered here so the openai_responses copy doesn't drift.
# ======================================================================


def test_classify_builtin_event_in_openai_responses_module():
    assert _classify_builtin_event("response.web_search_call.searching") == (
        "web_search_call", "searching",
    )
    assert _classify_builtin_event("response.mcp_call_arguments.delta") == (
        "mcp_call", "arguments_delta",
    )
    assert _classify_builtin_event("response.code_interpreter_call.code.delta") == (
        "code_interpreter_call", "code_delta",
    )
    assert _classify_builtin_event("response.output_text.delta") is None


# ======================================================================
# _stream captures built-in progress + items
# ======================================================================


@pytest.mark.asyncio
async def test_stream_captures_web_search_progress_and_item():
    provider = _make_provider()
    events = [
        _evt("response.web_search_call.in_progress", item_id="ws_1"),
        _evt("response.web_search_call.searching", item_id="ws_1"),
        _evt("response.web_search_call.completed", item_id="ws_1"),
        _evt(
            "response.output_item.done",
            item={
                "type": "web_search_call",
                "id": "ws_1",
                "status": "completed",
                "action": {"type": "search", "query": "cats"},
            },
        ),
    ]
    provider._client = SimpleNamespace(
        responses=SimpleNamespace(
            stream=lambda **kw: _FakeStream(events, _final())
        )
    )
    text, tool_calls, _usage = await provider._stream({"model": "gpt-5"}, None)
    assert text == ""
    assert tool_calls == []
    # Drain progress — three phases.
    progress = provider.consume_builtin_tool_events()
    assert [p["phase"] for p in progress] == [
        "in_progress", "searching", "completed",
    ]
    # consume_extra_blocks folds reasoning + built-in items.
    blocks = provider.consume_extra_blocks()
    assert len(blocks) == 1 and blocks[0]["type"] == "web_search_call"
    assert blocks[0]["action"]["query"] == "cats"


@pytest.mark.asyncio
async def test_stream_code_interpreter_assembles_deltas():
    provider = _make_provider()
    events = [
        _evt("response.code_interpreter_call.code.delta", item_id="ci_1", delta="x = 1\n"),
        _evt("response.code_interpreter_call.code.delta", item_id="ci_1", delta="print(x)\n"),
        _evt("response.code_interpreter_call.code.done", item_id="ci_1"),
        _evt(
            "response.output_item.done",
            item={
                "type": "code_interpreter_call",
                "id": "ci_1",
                "status": "completed",
            },
        ),
    ]
    provider._client = SimpleNamespace(
        responses=SimpleNamespace(stream=lambda **kw: _FakeStream(events, _final()))
    )
    await provider._stream({"model": "gpt-5"}, None)
    blocks = provider.consume_extra_blocks()
    assert blocks[0]["code"] == "x = 1\nprint(x)\n"


@pytest.mark.asyncio
async def test_stream_mcp_failed_does_not_raise():
    provider = _make_provider()
    events = [
        _evt("response.mcp_call.in_progress", item_id="mcp_1"),
        _evt(
            "response.mcp_call.failed",
            item_id="mcp_1",
            error={"message": "offline"},
        ),
        _evt(
            "response.output_item.done",
            item={"type": "mcp_call", "id": "mcp_1", "status": "failed"},
        ),
    ]
    provider._client = SimpleNamespace(
        responses=SimpleNamespace(stream=lambda **kw: _FakeStream(events, _final()))
    )
    text, _tcs, _u = await provider._stream({"model": "gpt-5"}, None)
    assert text == ""
    progress = provider.consume_builtin_tool_events()
    phases = [p["phase"] for p in progress]
    assert "failed" in phases
    blocks = provider.consume_extra_blocks()
    assert blocks[0]["type"] == "mcp_call" and blocks[0]["status"] == "failed"


# ======================================================================
# Request body passthrough
# ======================================================================


def test_format_tool_for_request_passes_web_search_dict_verbatim():
    assert _format_tool_for_request({"type": "web_search"}) == {"type": "web_search"}


def test_format_tool_for_request_wraps_function_tool():
    t = Tool(
        name="bash",
        description="shell",
        func=lambda **kw: None,
        schema={"type": "object", "properties": {}, "required": []},
    )
    shaped = _format_tool_for_request(t)
    assert shaped["type"] == "function" and shaped["name"] == "bash"


def test_format_tool_for_request_uses_builtin_dict_from_tool():
    t = Tool(
        name="web_search",
        description="builtin",
        func=lambda **kw: None,
        schema={"type": "object", "properties": {}, "required": []},
        builtin_dict={"type": "web_search"},
    )
    assert _format_tool_for_request(t) == {"type": "web_search"}


# ======================================================================
# _convert_assistant round-trips built-in items
# ======================================================================


def test_convert_assistant_replays_file_search_call():
    msg = Message(
        role="assistant",
        content=[
            {
                "type": "file_search_call",
                "id": "fs_1",
                "status": "completed",
                "queries": ["sql tutorials"],
            },
            {"type": "text", "text": "here's what I found"},
        ],
    )
    items = _convert_assistant(msg)
    assert items[0]["type"] == "file_search_call"
    assert items[0]["queries"] == ["sql tutorials"]
    assert items[1]["type"] == "message"


# ======================================================================
# Non-stream path captures built-in items
# ======================================================================


@pytest.mark.asyncio
async def test_non_stream_captures_builtin_items():
    provider = _make_provider()
    resp = SimpleNamespace(
        output=[
            {
                "type": "web_search_call",
                "id": "ws_1",
                "status": "completed",
            },
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "ok"}],
            },
        ],
        usage=SimpleNamespace(
            input_tokens=5,
            output_tokens=1,
            input_tokens_details=None,
            output_tokens_details=None,
        ),
    )

    async def _create(**kw):
        return resp

    provider._client = SimpleNamespace(responses=SimpleNamespace(create=_create))
    text, _tcs, _u = await provider._non_stream({"model": "gpt-5"})
    assert text == "ok"
    blocks = provider.consume_extra_blocks()
    assert len(blocks) == 1 and blocks[0]["type"] == "web_search_call"
