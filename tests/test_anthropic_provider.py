from types import SimpleNamespace

import pytest

from nutshell.core.types import Message
from nutshell.llm_engine.providers.anthropic import AnthropicProvider


class _FakeStream:
    def __init__(self, events, final_message):
        self._events = events
        self._final_message = final_message

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        async def _gen():
            for event in self._events:
                yield event

        return _gen()

    async def get_final_message(self):
        return self._final_message


@pytest.mark.asyncio
async def test_complete_streams_thinking_and_text_chunks_in_order():
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.max_tokens = 123

    final_message = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="reasoning..."),
            SimpleNamespace(type="text", text="final answer"),
        ]
    )
    events = [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="thinking_delta", thinking="reasoning..."),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="final answer"),
        ),
    ]
    provider._client = SimpleNamespace(
        messages=SimpleNamespace(
            stream=lambda **kwargs: _FakeStream(events, final_message),
        )
    )

    chunks: list[str] = []
    content, tool_calls, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="system",
        model="claude-test",
        on_text_chunk=chunks.append,
    )

    assert chunks == ["reasoning...", "final answer"]
    assert content == "final answer"
    assert tool_calls == []


@pytest.mark.asyncio
async def test_complete_falls_back_to_final_thinking_block_when_stream_has_no_thinking_delta():
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.max_tokens = 123

    final_message = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="reasoning..."),
            SimpleNamespace(type="text", text="final answer"),
        ]
    )
    events = [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="final answer"),
        ),
    ]
    provider._client = SimpleNamespace(
        messages=SimpleNamespace(
            stream=lambda **kwargs: _FakeStream(events, final_message),
        )
    )

    chunks: list[str] = []
    content, tool_calls, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="system",
        model="claude-test",
        on_text_chunk=chunks.append,
    )

    assert chunks == ["final answer", "reasoning..."]
    assert content == "final answer"
    assert tool_calls == []


@pytest.mark.asyncio
async def test_complete_collects_tool_calls_from_final_message():
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.max_tokens = 123

    final_message = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text=""),
            SimpleNamespace(type="tool_use", id="tool-1", name="search", input={"q": "abc"}),
        ]
    )
    provider._client = SimpleNamespace(
        messages=SimpleNamespace(
            create=_async_return(final_message),
        )
    )

    content, tool_calls, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="system",
        model="claude-test",
    )

    assert content == ""
    assert len(tool_calls) == 1
    assert tool_calls[0].id == "tool-1"
    assert tool_calls[0].name == "search"
    assert tool_calls[0].input == {"q": "abc"}


def _async_return(value):
    async def _inner(**kwargs):
        return value

    return _inner
