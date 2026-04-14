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
    assert items[0]["output"] == "a b"


def test_convert_tool_result_ignores_non_list_content():
    from butterfly.llm_engine.providers.openai_responses import _convert_tool_result

    msg = Message(role="tool", content="flat")
    assert _convert_tool_result(msg) == []


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
