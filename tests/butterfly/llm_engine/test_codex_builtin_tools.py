"""Tests for Responses API / Codex built-in-tool plumbing.

Covers:
  * SSE parser routes ``response.<tool>_call.*`` into the new progress + item
    channels.
  * ``_build_request_body`` passes built-in tool specs through verbatim.
  * ``_convert_assistant`` replays captured built-in items verbatim on the
    next turn's ``input[]``.
  * Toolhub stubs (``web_search``, ``file_search``, ``code_interpreter``)
    load, raise on direct execution, and expose a ``to_builtin_dict()``
    shape.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from butterfly.core.tool import Tool
from butterfly.core.types import Message
from butterfly.llm_engine.providers.codex import (
    CodexProvider,
    _build_request_body,
    _classify_builtin_event,
    _convert_assistant,
    _format_tool_for_request,
    _parse_sse_stream,
    _tool_to_responses_api,
)
from butterfly.tool_engine.loader import ToolLoader


# ======================================================================
# SSE parser — built-in tool events
# ======================================================================


class _FakeSSEResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


def _sse(event: dict) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


def _drive(coro):
    return asyncio.run(coro)


# ── classifier ─────────────────────────────────────────────────────────


def test_classify_builtin_event_recognises_all_families():
    # Positive
    assert _classify_builtin_event("response.web_search_call.in_progress") == (
        "web_search_call", "in_progress",
    )
    assert _classify_builtin_event("response.file_search_call.searching") == (
        "file_search_call", "searching",
    )
    assert _classify_builtin_event("response.code_interpreter_call.code.delta") == (
        "code_interpreter_call", "code_delta",
    )
    assert _classify_builtin_event("response.code_interpreter_call.code.done") == (
        "code_interpreter_call", "code_done",
    )
    assert _classify_builtin_event("response.image_generation_call.partial_image") == (
        "image_generation_call", "partial_image",
    )
    assert _classify_builtin_event("response.mcp_call.failed") == (
        "mcp_call", "failed",
    )
    assert _classify_builtin_event("response.mcp_call_arguments.delta") == (
        "mcp_call", "arguments_delta",
    )
    # Negative — unrelated or malformed etypes return None.
    assert _classify_builtin_event("response.output_text.delta") is None
    assert _classify_builtin_event("") is None
    assert _classify_builtin_event("not-an-event") is None


# ── full stream parses with built-in events ────────────────────────────


def _make_completed(usage: dict | None = None) -> bytes:
    return _sse({"type": "response.completed", "response": {"usage": usage or {}}})


@pytest.mark.asyncio
async def test_sse_web_search_lifecycle_emits_progress_and_captures_item():
    chunks = [
        _sse({"type": "response.web_search_call.in_progress", "item_id": "ws_1"}),
        _sse({"type": "response.web_search_call.searching", "item_id": "ws_1"}),
        _sse({"type": "response.web_search_call.completed", "item_id": "ws_1"}),
        _sse({
            "type": "response.output_item.done",
            "item": {
                "type": "web_search_call",
                "id": "ws_1",
                "status": "completed",
                "action": {"type": "search", "query": "cats"},
            },
        }),
        _make_completed(),
    ]
    text, tcs, _u, _rs, builtin_items, builtin_progress = await _parse_sse_stream(
        _FakeSSEResponse(chunks), None
    )
    assert text == ""
    assert tcs == []
    # Progress — three events, each with payload preserved minus type.
    phases = [p["phase"] for p in builtin_progress]
    assert phases == ["in_progress", "searching", "completed"]
    assert all(p["tool_type"] == "web_search_call" for p in builtin_progress)
    assert builtin_progress[0]["payload"]["item_id"] == "ws_1"
    # Captured item preserves fields.
    assert len(builtin_items) == 1
    assert builtin_items[0]["type"] == "web_search_call"
    assert builtin_items[0]["id"] == "ws_1"
    assert builtin_items[0]["status"] == "completed"
    assert builtin_items[0]["action"]["query"] == "cats"


@pytest.mark.asyncio
async def test_sse_file_search_lifecycle_captured():
    chunks = [
        _sse({"type": "response.file_search_call.in_progress", "item_id": "fs_1"}),
        _sse({"type": "response.file_search_call.searching", "item_id": "fs_1"}),
        _sse({"type": "response.file_search_call.completed", "item_id": "fs_1"}),
        _sse({
            "type": "response.output_item.done",
            "item": {
                "type": "file_search_call",
                "id": "fs_1",
                "status": "completed",
                "queries": ["python type hints"],
            },
        }),
        _make_completed(),
    ]
    _t, _tcs, _u, _rs, items, progress = await _parse_sse_stream(
        _FakeSSEResponse(chunks), None
    )
    assert len(items) == 1 and items[0]["type"] == "file_search_call"
    assert items[0]["queries"] == ["python type hints"]
    assert [p["phase"] for p in progress] == ["in_progress", "searching", "completed"]


@pytest.mark.asyncio
async def test_sse_code_interpreter_assembles_code_deltas():
    chunks = [
        _sse({"type": "response.code_interpreter_call.in_progress", "item_id": "ci_1"}),
        _sse({
            "type": "response.code_interpreter_call.code.delta",
            "item_id": "ci_1",
            "delta": "import math\n",
        }),
        _sse({
            "type": "response.code_interpreter_call.code.delta",
            "item_id": "ci_1",
            "delta": "print(math.pi)\n",
        }),
        _sse({
            "type": "response.code_interpreter_call.code.done",
            "item_id": "ci_1",
        }),
        _sse({"type": "response.code_interpreter_call.interpreting", "item_id": "ci_1"}),
        _sse({"type": "response.code_interpreter_call.completed", "item_id": "ci_1"}),
        _sse({
            "type": "response.output_item.done",
            "item": {
                "type": "code_interpreter_call",
                "id": "ci_1",
                "status": "completed",
                # Deliberately omit ``code`` so the parser's fallback path
                # (assemble from deltas) is exercised.
            },
        }),
        _make_completed(),
    ]
    _t, _tcs, _u, _rs, items, progress = await _parse_sse_stream(
        _FakeSSEResponse(chunks), None
    )
    assert len(items) == 1 and items[0]["type"] == "code_interpreter_call"
    assert items[0]["code"] == "import math\nprint(math.pi)\n"
    # Phases flattened — code_delta / code_done collapse the dot.
    assert "code_delta" in {p["phase"] for p in progress}
    assert "code_done" in {p["phase"] for p in progress}


@pytest.mark.asyncio
async def test_sse_image_generation_partial_image_captured_as_progress():
    chunks = [
        _sse({"type": "response.image_generation_call.generating", "item_id": "ig_1"}),
        _sse({
            "type": "response.image_generation_call.partial_image",
            "item_id": "ig_1",
            "partial_image_index": 0,
            "b64_json": "...",
        }),
        _sse({"type": "response.image_generation_call.completed", "item_id": "ig_1"}),
        _sse({
            "type": "response.output_item.done",
            "item": {
                "type": "image_generation_call",
                "id": "ig_1",
                "status": "completed",
            },
        }),
        _make_completed(),
    ]
    _t, _tcs, _u, _rs, items, progress = await _parse_sse_stream(
        _FakeSSEResponse(chunks), None
    )
    phases = [p["phase"] for p in progress]
    assert "generating" in phases
    assert "partial_image" in phases
    assert items[0]["type"] == "image_generation_call"


@pytest.mark.asyncio
async def test_sse_mcp_call_failed_represented_as_progress_not_raised():
    chunks = [
        _sse({"type": "response.mcp_call.in_progress", "item_id": "mcp_1"}),
        _sse({
            "type": "response.mcp_call_arguments.delta",
            "item_id": "mcp_1",
            "delta": "{\"q\":",
        }),
        _sse({
            "type": "response.mcp_call.failed",
            "item_id": "mcp_1",
            "error": {"message": "server offline"},
        }),
        _sse({
            "type": "response.output_item.done",
            "item": {
                "type": "mcp_call",
                "id": "mcp_1",
                "status": "failed",
            },
        }),
        _make_completed(),
    ]
    # Must NOT raise even though phase == "failed".
    _t, _tcs, _u, _rs, items, progress = await _parse_sse_stream(
        _FakeSSEResponse(chunks), None
    )
    phases = [p["phase"] for p in progress]
    assert "failed" in phases
    assert "arguments_delta" in phases
    assert items[0]["type"] == "mcp_call"
    assert items[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_sse_mcp_call_success_and_list_tools_captured_distinctly():
    chunks = [
        _sse({
            "type": "response.output_item.done",
            "item": {
                "type": "mcp_list_tools",
                "id": "mcp_lt_1",
                "tools": [{"name": "weather"}],
            },
        }),
        _sse({
            "type": "response.output_item.done",
            "item": {
                "type": "mcp_call",
                "id": "mcp_1",
                "status": "completed",
                "result": {"temp": 70},
            },
        }),
        _make_completed(),
    ]
    _t, _tcs, _u, _rs, items, _progress = await _parse_sse_stream(
        _FakeSSEResponse(chunks), None
    )
    types = sorted(i["type"] for i in items)
    assert types == ["mcp_call", "mcp_list_tools"]


@pytest.mark.asyncio
async def test_sse_malformed_builtin_event_without_item_id_doesnt_crash():
    chunks = [
        # No item_id — must not raise, progress still recorded.
        _sse({"type": "response.web_search_call.searching"}),
        # code_delta with no delta body — also no-op (nothing to accumulate).
        _sse({"type": "response.code_interpreter_call.code.delta"}),
        _make_completed(),
    ]
    _t, _tcs, _u, _rs, items, progress = await _parse_sse_stream(
        _FakeSSEResponse(chunks), None
    )
    assert items == []
    assert len(progress) == 2
    assert progress[0]["phase"] == "searching"
    assert progress[1]["phase"] == "code_delta"


# ======================================================================
# Request body passthrough
# ======================================================================


def test_build_request_body_passes_web_search_dict_verbatim():
    body = _build_request_body(
        "gpt-5.4", "sys", [Message(role="user", content="hi")],
        tools=[{"type": "web_search"}],
    )
    assert body["tools"] == [{"type": "web_search"}]


def test_build_request_body_mixes_function_tool_and_builtin_dict():
    class _Fn:
        def to_api_dict(self):
            return {"name": "bash", "description": "d", "input_schema": {"type": "object"}}

        def to_builtin_dict(self):
            return None

    body = _build_request_body(
        "gpt-5.4", "sys", [Message(role="user", content="hi")],
        tools=[_Fn(), {"type": "web_search"}],
    )
    shapes = body["tools"]
    # Function tool wrapped, built-in passed through.
    assert shapes[0]["type"] == "function" and shapes[0]["name"] == "bash"
    assert shapes[1] == {"type": "web_search"}


def test_format_tool_for_request_handles_tool_object_with_builtin_dict():
    t = Tool(
        name="web_search",
        description="builtin",
        func=lambda **kw: None,
        schema={"type": "object", "properties": {}, "required": []},
        builtin_dict={"type": "web_search"},
    )
    # Tool.to_builtin_dict returns a copy → _format_tool_for_request uses it.
    assert _format_tool_for_request(t) == {"type": "web_search"}
    # Plain function tool still gets function-wrapped.
    fn_t = Tool(
        name="bash",
        description="shell",
        func=lambda **kw: None,
        schema={"type": "object", "properties": {}, "required": []},
    )
    shaped = _format_tool_for_request(fn_t)
    assert shaped["type"] == "function" and shaped["name"] == "bash"


# ======================================================================
# Round-trip replay
# ======================================================================


def test_convert_assistant_replays_web_search_call_verbatim():
    msg = Message(
        role="assistant",
        content=[
            {
                "type": "web_search_call",
                "id": "ws_1",
                "status": "completed",
                "action": {"type": "search", "query": "cats"},
            },
            {"type": "text", "text": "cats are fuzzy"},
        ],
    )
    items = _convert_assistant(msg)
    # Order: built-in item → text message (text flushed after the item).
    assert items[0]["type"] == "web_search_call"
    assert items[0]["id"] == "ws_1"
    assert items[0]["action"]["query"] == "cats"
    assert items[1]["type"] == "message"


def test_convert_assistant_strips_none_fields_from_builtin_item():
    """Responses API rejects schema-invalid ``null`` on built-in items."""
    msg = Message(
        role="assistant",
        content=[
            {
                "type": "file_search_call",
                "id": "fs_1",
                "status": "completed",
                "queries": None,  # null would break the server validator
                "_internal_tid": 123,  # bookkeeping key — also stripped
            },
        ],
    )
    items = _convert_assistant(msg)
    assert items == [{
        "type": "file_search_call",
        "id": "fs_1",
        "status": "completed",
    }]


# ======================================================================
# consume_builtin_tool_events + consume_extra_blocks semantics
# ======================================================================


def test_consume_builtin_tool_events_drains_and_is_idempotent():
    p = CodexProvider.__new__(CodexProvider)
    p._pending_reasoning = []
    p._pending_builtin_items = [{"type": "web_search_call", "id": "ws_1"}]
    p._pending_builtin_progress = [
        {"tool_type": "web_search_call", "phase": "searching", "payload": {}}
    ]
    # First drain returns progress events.
    evts = p.consume_builtin_tool_events()
    assert len(evts) == 1 and evts[0]["phase"] == "searching"
    # Second drain empty.
    assert p.consume_builtin_tool_events() == []
    # consume_extra_blocks folds reasoning + builtin items together.
    blocks = p.consume_extra_blocks()
    assert blocks == [{"type": "web_search_call", "id": "ws_1"}]
    assert p.consume_extra_blocks() == []


# ======================================================================
# Toolhub stubs
# ======================================================================


def test_web_search_toolhub_stub_loads_and_carries_builtin_dict():
    loader = ToolLoader()
    tool = loader.load_from_toolhub("web_search")
    assert tool is not None
    assert tool.name == "web_search"
    assert tool.is_builtin is True
    assert tool.to_builtin_dict() == {"type": "web_search"}


def test_file_search_toolhub_stub_loads_with_vector_store_field():
    loader = ToolLoader()
    tool = loader.load_from_toolhub("file_search")
    assert tool is not None
    assert tool.is_builtin is True
    spec = tool.to_builtin_dict()
    assert spec["type"] == "file_search"
    assert "vector_store_ids" in spec


def test_code_interpreter_toolhub_stub_loads_with_container_field():
    loader = ToolLoader()
    tool = loader.load_from_toolhub("code_interpreter")
    assert tool is not None
    assert tool.is_builtin is True
    spec = tool.to_builtin_dict()
    assert spec["type"] == "code_interpreter"
    assert "container" in spec


def test_builtin_stub_execute_raises_not_implemented():
    loader = ToolLoader()
    for name in ("web_search", "file_search", "code_interpreter"):
        tool = loader.load_from_toolhub(name)
        assert tool is not None
        with pytest.raises(NotImplementedError) as exc:
            _drive(tool.execute())
        assert "provider-native" in str(exc.value)


def test_builtin_tool_round_trips_through_format_helper():
    loader = ToolLoader()
    tool = loader.load_from_toolhub("web_search")
    assert tool is not None
    assert _format_tool_for_request(tool) == {"type": "web_search"}
    # Ensure to_api_dict still returns the normal function-tool shape (used
    # for docs / diagnostics) so the built-in is visible to logging paths.
    api = tool.to_api_dict()
    assert api["name"] == "web_search"


# ======================================================================
# Guard: old function-tool path not broken
# ======================================================================


def test_function_tool_still_wrapped_as_type_function():
    class _Fn:
        def to_api_dict(self):
            return {"name": "echo", "description": "echo", "input_schema": {"type": "object"}}

    shape = _tool_to_responses_api(_Fn())
    assert shape["type"] == "function" and shape["name"] == "echo"
