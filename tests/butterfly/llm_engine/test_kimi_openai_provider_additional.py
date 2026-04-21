from __future__ import annotations

from types import SimpleNamespace

import pytest

from butterfly.core.types import Message
from butterfly.llm_engine.providers.kimi import KimiOpenAIProvider
from butterfly.llm_engine.providers.openai_api import _build_messages
from butterfly.llm_engine.registry import provider_name


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()


def _make_provider() -> KimiOpenAIProvider:
    provider = KimiOpenAIProvider.__new__(KimiOpenAIProvider)
    provider.max_tokens = 8096
    provider._client = None
    return provider


@pytest.mark.asyncio
async def test_kimi_openai_streaming_tool_calls_and_choice_usage_fallback():
    provider = _make_provider()

    chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="tc-kimi-1",
                                function=SimpleNamespace(name="search", arguments='{"q":'),
                            )
                        ],
                    ),
                    usage=SimpleNamespace(
                        prompt_tokens=80,
                        completion_tokens=7,
                        cached_tokens=30,
                        prompt_tokens_details=None,
                        completion_tokens_details=SimpleNamespace(reasoning_tokens=4),
                    ),
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id=None,
                                function=SimpleNamespace(name=None, arguments=' "butterfly"}'),
                            )
                        ],
                    ),
                    usage=None,
                )
            ],
            usage=None,
        ),
    ]

    async def _create(**kwargs):
        return _FakeStream(chunks)

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )

    text, tool_calls, usage = await provider.complete(
        messages=[Message(role="user", content="find butterfly")],
        tools=[],
        system_prompt="sys",
        model="kimi-for-coding",
        on_text_chunk=lambda _chunk: None,
    )

    assert text == ""
    assert len(tool_calls) == 1
    assert tool_calls[0].id == "tc-kimi-1"
    assert tool_calls[0].name == "search"
    assert tool_calls[0].input == {"q": "butterfly"}
    assert usage.input_tokens == 50
    assert usage.cache_read_tokens == 30
    assert usage.output_tokens == 7
    assert usage.reasoning_tokens == 4


def test_openai_message_builder_sanitizes_reasoning_only_assistant_for_kimi_fallback():
    msgs = _build_messages(
        "sys",
        [
            Message(role="user", content="hi"),
            Message(
                role="assistant",
                content=[
                    {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
                ],
            ),
            Message(role="user", content="continue"),
        ],
    )

    assistant = next(m for m in msgs if m["role"] == "assistant")
    assert assistant["content"] == "[continued]"
    assert "tool_calls" not in assistant


def test_provider_name_for_kimi_openai_variant():
    provider = KimiOpenAIProvider.__new__(KimiOpenAIProvider)
    assert provider_name(provider) == "kimi-coding-plan"
