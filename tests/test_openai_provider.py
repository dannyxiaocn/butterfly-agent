"""Tests for OpenAIProvider — message conversion, tool format, streaming, usage."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from nutshell.core.types import Message, TokenUsage, ToolCall
from nutshell.core.tool import Tool
from nutshell.llm_engine.providers.openai_provider import (
    OpenAIProvider,
    _build_messages,
    _extract_usage_from_obj,
    _parse_response,
    _tc_map_to_list,
    _tool_to_openai,
)


# ── helpers ──────────────────────────────────────────────────────────


def _make_provider() -> OpenAIProvider:
    """Create a provider without hitting the real OpenAI SDK constructor."""
    p = OpenAIProvider.__new__(OpenAIProvider)
    p.max_tokens = 8096
    p._client = None  # will be patched per-test
    return p


def _dummy_tool(name: str = "search", desc: str = "Search the web", schema: dict | None = None) -> Tool:
    async def _noop(**kw: Any) -> str:
        return ""
    return Tool(
        name=name,
        description=desc,
        func=_noop,
        schema=schema or {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    )


def _make_response(
    content: str = "",
    tool_calls: list[dict] | None = None,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    cached_tokens: int = 0,
) -> SimpleNamespace:
    """Build a fake ChatCompletion response."""
    tc_objs = None
    if tool_calls:
        tc_objs = [
            SimpleNamespace(
                id=tc["id"],
                type="function",
                function=SimpleNamespace(
                    name=tc["name"],
                    arguments=json.dumps(tc.get("arguments", {})),
                ),
            )
            for tc in tool_calls
        ]

    msg = SimpleNamespace(content=content or None, tool_calls=tc_objs)
    choice = SimpleNamespace(message=msg, finish_reason="stop")

    prompt_details = SimpleNamespace(cached_tokens=cached_tokens) if cached_tokens else None
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_tokens_details=prompt_details,
    )
    return SimpleNamespace(choices=[choice], usage=usage)


class _AsyncStreamFake:
    """Simulates an AsyncStream of ChatCompletionChunk objects."""

    def __init__(self, chunks: list[SimpleNamespace]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def _gen():
            for c in self._chunks:
                yield c
        return _gen()


# ── 1. system prompt as first message ────────────────────────────────


def test_system_prompt_first_message():
    msgs = _build_messages("You are helpful.", [Message(role="user", content="hi")])
    assert msgs[0] == {"role": "system", "content": "You are helpful."}
    assert msgs[1] == {"role": "user", "content": "hi"}


# ── 2. cache_prefix merges into system ───────────────────────────────


def test_system_prompt_with_cache_prefix():
    msgs = _build_messages("dynamic", [Message(role="user", content="x")], cache_prefix="static")
    assert msgs[0] == {"role": "system", "content": "static\ndynamic"}


# ── 3. user message conversion ───────────────────────────────────────


def test_user_messages_plain_string():
    msgs = _build_messages("sys", [
        Message(role="user", content="hello"),
        Message(role="user", content="world"),
    ])
    assert msgs[1]["content"] == "hello"
    assert msgs[2]["content"] == "world"


# ── 4. assistant message with tool_use blocks ────────────────────────


def test_assistant_message_with_tool_calls():
    content = [
        {"type": "text", "text": "Let me search."},
        {"type": "tool_use", "id": "tc1", "name": "search", "input": {"q": "nutshell"}},
    ]
    msgs = _build_messages("sys", [Message(role="assistant", content=content)])
    asst = msgs[1]
    assert asst["role"] == "assistant"
    assert asst["content"] == "Let me search."
    assert len(asst["tool_calls"]) == 1
    assert asst["tool_calls"][0]["id"] == "tc1"
    assert asst["tool_calls"][0]["function"]["name"] == "search"
    assert json.loads(asst["tool_calls"][0]["function"]["arguments"]) == {"q": "nutshell"}


# ── 5. tool result message conversion ────────────────────────────────


def test_tool_result_message():
    content = [
        {"type": "tool_result", "tool_use_id": "tc1", "content": "42 results found"},
    ]
    msgs = _build_messages("sys", [Message(role="tool", content=content)])
    tool_msg = msgs[1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "tc1"
    assert tool_msg["content"] == "42 results found"


# ── 6. tool schema conversion ────────────────────────────────────────


def test_tool_to_openai_format():
    t = _dummy_tool()
    result = _tool_to_openai(t)
    assert result["type"] == "function"
    assert result["function"]["name"] == "search"
    assert result["function"]["description"] == "Search the web"
    assert result["function"]["parameters"]["type"] == "object"
    assert "q" in result["function"]["parameters"]["properties"]


# ── 7. non-streaming complete ────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_non_streaming_text():
    provider = _make_provider()
    resp = _make_response(content="Hello world", prompt_tokens=15, completion_tokens=3)

    async def _create(**kw: Any):
        return resp

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )

    text, tcs, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="gpt-5.4",
    )
    assert text == "Hello world"
    assert tcs == []
    assert usage.input_tokens == 15
    assert usage.output_tokens == 3


# ── 8. non-streaming with tool calls ─────────────────────────────────


@pytest.mark.asyncio
async def test_complete_non_streaming_tool_calls():
    provider = _make_provider()
    resp = _make_response(
        content="",
        tool_calls=[{"id": "tc-1", "name": "bash", "arguments": {"command": "ls"}}],
    )

    async def _create(**kw: Any):
        return resp

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )

    text, tcs, usage = await provider.complete(
        messages=[Message(role="user", content="list files")],
        tools=[_dummy_tool("bash", "Run command")],
        system_prompt="sys",
        model="gpt-5.4",
    )
    assert text == ""
    assert len(tcs) == 1
    assert tcs[0].name == "bash"
    assert tcs[0].input == {"command": "ls"}


# ── 9. streaming complete with text chunks ────────────────────────────


@pytest.mark.asyncio
async def test_complete_streaming_text_chunks():
    provider = _make_provider()

    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello ", tool_calls=None))],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="world", tool_calls=None))],
            usage=None,
        ),
        # Final chunk with usage, no choices
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(
                prompt_tokens=20,
                completion_tokens=10,
                prompt_tokens_details=None,
            ),
        ),
    ]

    async def _create(**kw: Any):
        assert kw.get("stream") is True
        return _AsyncStreamFake(chunks)

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )

    streamed: list[str] = []
    text, tcs, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="gpt-5.4",
        on_text_chunk=streamed.append,
    )
    assert streamed == ["Hello ", "world"]
    assert text == "Hello world"
    assert tcs == []
    assert usage.input_tokens == 20
    assert usage.output_tokens == 10


# ── 10. streaming with tool call deltas ───────────────────────────────


@pytest.mark.asyncio
async def test_complete_streaming_tool_calls():
    provider = _make_provider()

    chunks = [
        # First delta: tool_call start
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(
                    index=0,
                    id="tc-99",
                    function=SimpleNamespace(name="search", arguments='{"q":'),
                )],
            ))],
            usage=None,
        ),
        # Second delta: argument continuation
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(
                    index=0,
                    id=None,
                    function=SimpleNamespace(name=None, arguments=' "test"}'),
                )],
            ))],
            usage=None,
        ),
        # Final usage
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=8, prompt_tokens_details=None),
        ),
    ]

    async def _create(**kw: Any):
        return _AsyncStreamFake(chunks)

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )

    streamed: list[str] = []
    text, tcs, usage = await provider.complete(
        messages=[Message(role="user", content="search test")],
        tools=[_dummy_tool()],
        system_prompt="sys",
        model="gpt-5.4",
        on_text_chunk=streamed.append,
    )
    assert text == ""
    assert streamed == []
    assert len(tcs) == 1
    assert tcs[0].id == "tc-99"
    assert tcs[0].name == "search"
    assert tcs[0].input == {"q": "test"}


# ── 11. TokenUsage extraction with cached tokens ─────────────────────


def test_extract_usage_with_cache():
    usage_obj = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=50,
        prompt_tokens_details=SimpleNamespace(cached_tokens=30),
    )
    usage = _extract_usage_from_obj(usage_obj)
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cache_read_tokens == 30
    assert usage.cache_write_tokens == 0


# ── 12. TokenUsage extraction without details ────────────────────────


def test_extract_usage_no_cache():
    usage_obj = SimpleNamespace(
        prompt_tokens=40,
        completion_tokens=20,
        prompt_tokens_details=None,
    )
    usage = _extract_usage_from_obj(usage_obj)
    assert usage.input_tokens == 40
    assert usage.output_tokens == 20
    assert usage.cache_read_tokens == 0


# ── 13. _tc_map_to_list ordering ─────────────────────────────────────


def test_tc_map_to_list_sorts_by_index():
    tc_map = {
        2: {"id": "b", "name": "tool_b", "arguments": "{}"},
        0: {"id": "a", "name": "tool_a", "arguments": '{"x": 1}'},
    }
    result = _tc_map_to_list(tc_map)
    assert len(result) == 2
    assert result[0].id == "a"
    assert result[0].input == {"x": 1}
    assert result[1].id == "b"
    assert result[1].input == {}


# ── 14. _parse_response text only ────────────────────────────────────


def test_parse_response_text():
    resp = _make_response(content="answer", prompt_tokens=7, completion_tokens=3)
    text, tcs, usage = _parse_response(resp)
    assert text == "answer"
    assert tcs == []
    assert usage.total_tokens == 10


# ── 15. _parse_response with tool calls ──────────────────────────────


def test_parse_response_tool_calls():
    resp = _make_response(
        content="",
        tool_calls=[
            {"id": "t1", "name": "bash", "arguments": {"cmd": "echo hi"}},
            {"id": "t2", "name": "search", "arguments": {"q": "hello"}},
        ],
    )
    text, tcs, usage = _parse_response(resp)
    assert text == ""
    assert len(tcs) == 2
    assert tcs[0].name == "bash"
    assert tcs[1].name == "search"


# ── 16. tools passed to API kwargs ───────────────────────────────────


@pytest.mark.asyncio
async def test_complete_passes_tools_in_kwargs():
    provider = _make_provider()
    resp = _make_response(content="ok")
    captured: dict[str, Any] = {}

    async def _create(**kw: Any):
        captured.update(kw)
        return resp

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[_dummy_tool("search", "Search")],
        system_prompt="sys",
        model="gpt-5.4",
    )
    assert "tools" in captured
    assert captured["tools"][0]["type"] == "function"
    assert captured["tools"][0]["function"]["name"] == "search"


# ── 17. empty tools not sent ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_no_tools_key_when_empty():
    provider = _make_provider()
    resp = _make_response(content="ok")
    captured: dict[str, Any] = {}

    async def _create(**kw: Any):
        captured.update(kw)
        return resp

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="gpt-5.4",
    )
    assert "tools" not in captured


# ── 18. user list content flattened ───────────────────────────────────


def test_user_list_content_flattened():
    content = [
        {"type": "text", "text": "part1"},
        {"type": "text", "text": "part2"},
    ]
    msgs = _build_messages("sys", [Message(role="user", content=content)])
    assert msgs[1]["content"] == "part1part2"


# ── 19. empty system prompt still included ────────────────────────────


def test_empty_system_prompt_omitted():
    msgs = _build_messages("", [Message(role="user", content="hi")])
    # Empty system prompt should not produce a system message
    assert msgs[0]["role"] == "user"


# ── 20. registry resolves openai ──────────────────────────────────────


def test_registry_has_openai():
    from nutshell.llm_engine.registry import _REGISTRY
    assert "openai" in _REGISTRY
    mod, cls = _REGISTRY["openai"]
    assert cls == "OpenAIProvider"
    assert "openai_provider" in mod
