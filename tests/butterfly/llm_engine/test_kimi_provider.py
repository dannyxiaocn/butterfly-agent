"""Comprehensive unit tests for KimiForCodingProvider.

KimiForCodingProvider is a thin wrapper over AnthropicProvider that:
- Hardcodes base_url to ``_KIMI_BASE_URL`` (Moonshot's ``/coding/`` endpoint);
  there is no ``KIMI_BASE_URL`` override and no constructor parameter.
- Resolves api_key ONLY from ``KIMI_FOR_CODING_API_KEY``; there is no
  ``KIMI_API_KEY`` / ``MOONSHOT_API_KEY`` fallback.
- Uses extra_body for thinking (no betas header)
- Disables cache_control
"""
from __future__ import annotations

from typing import Any
from types import SimpleNamespace

import pytest

from butterfly.core.types import Message, TokenUsage, ToolCall
from butterfly.core.tool import Tool
from butterfly.llm_engine.providers.anthropic import AnthropicProvider
from butterfly.llm_engine.providers.kimi import KimiForCodingProvider, _KIMI_BASE_URL


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_provider() -> KimiForCodingProvider:
    """Create a Kimi provider without calling the real Anthropic SDK constructor."""
    p = KimiForCodingProvider.__new__(KimiForCodingProvider)
    p.max_tokens = 8096
    p._client = None  # patched per-test
    return p


async def _async_return(value: Any, captured: list | None = None):
    async def _inner(**kwargs):
        if captured is not None:
            captured.append(kwargs)
        return value
    return _inner


def _fake_client(captured: list) -> SimpleNamespace:
    """Return a fake Anthropic-style client that captures kwargs."""
    async def _create(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=SimpleNamespace(
                input_tokens=1,
                output_tokens=1,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )
    return SimpleNamespace(messages=SimpleNamespace(create=_create))


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


def _dummy_tool(name: str = "search", desc: str = "Search") -> Tool:
    async def _noop(**kw: Any) -> str:
        return ""
    return Tool(
        name=name,
        description=desc,
        func=_noop,
        schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    )


# ── 1. Constructor: base_url is hardcoded ─────────────────────────────────────


def test_kimi_base_url_is_hardcoded(monkeypatch):
    """``_KIMI_BASE_URL`` is always passed through — no env/ctor override exists."""
    monkeypatch.setenv("KIMI_FOR_CODING_API_KEY", "k")
    # Setting a stale KIMI_BASE_URL env var must have no effect (no longer read).
    monkeypatch.setenv("KIMI_BASE_URL", "https://ignored.kimi.com/")
    captured = {}

    def _fake_init(self, *, api_key=None, max_tokens=8096, base_url=None, default_headers=None):
        captured["base_url"] = base_url

    monkeypatch.setattr(AnthropicProvider, "__init__", _fake_init)
    KimiForCodingProvider()
    assert captured["base_url"] == _KIMI_BASE_URL


def test_kimi_ctor_rejects_base_url_kwarg(monkeypatch):
    """The ``base_url`` constructor parameter was removed — passing it errors."""
    monkeypatch.setenv("KIMI_FOR_CODING_API_KEY", "k")
    with pytest.raises(TypeError):
        KimiForCodingProvider(base_url="https://explicit.kimi.com/")  # type: ignore[call-arg]


# ── 2. Constructor: API key resolution (only KIMI_FOR_CODING_API_KEY) ─────────


def test_kimi_api_key_explicit(monkeypatch):
    monkeypatch.delenv("KIMI_FOR_CODING_API_KEY", raising=False)
    captured = {}

    def _fake_init(self, *, api_key=None, max_tokens=8096, base_url=None, default_headers=None):
        captured["api_key"] = api_key

    monkeypatch.setattr(AnthropicProvider, "__init__", _fake_init)
    KimiForCodingProvider(api_key="explicit-key")
    assert captured["api_key"] == "explicit-key"


def test_kimi_api_key_from_env(monkeypatch):
    monkeypatch.setenv("KIMI_FOR_CODING_API_KEY", "primary-key")
    captured = {}

    def _fake_init(self, *, api_key=None, max_tokens=8096, base_url=None, default_headers=None):
        captured["api_key"] = api_key

    monkeypatch.setattr(AnthropicProvider, "__init__", _fake_init)
    KimiForCodingProvider()
    assert captured["api_key"] == "primary-key"


def test_kimi_api_key_legacy_env_var_is_ignored(monkeypatch):
    """``KIMI_API_KEY`` used to be a fallback; it is NO LONGER honored.

    The provider only supports the Kimi For Coding path. Setting the legacy
    variable with the canonical one unset must raise ``AuthError`` (no silent
    fallback to the legacy key).
    """
    monkeypatch.delenv("KIMI_FOR_CODING_API_KEY", raising=False)
    monkeypatch.setenv("KIMI_API_KEY", "legacy-value")
    from butterfly.llm_engine.errors import AuthError

    with pytest.raises(AuthError):
        KimiForCodingProvider()


def test_kimi_api_key_explicit_overrides_env(monkeypatch):
    monkeypatch.setenv("KIMI_FOR_CODING_API_KEY", "env-key")
    captured = {}

    def _fake_init(self, *, api_key=None, max_tokens=8096, base_url=None, default_headers=None):
        captured["api_key"] = api_key

    monkeypatch.setattr(AnthropicProvider, "__init__", _fake_init)
    KimiForCodingProvider(api_key="explicit-key")
    assert captured["api_key"] == "explicit-key"


# ── 3. Constructor: max_tokens default ────────────────────────────────────────


def test_kimi_max_tokens_default(monkeypatch):
    monkeypatch.setenv("KIMI_FOR_CODING_API_KEY", "k")
    captured = {}

    def _fake_init(self, *, api_key=None, max_tokens=8096, base_url=None, default_headers=None):
        captured["max_tokens"] = max_tokens

    monkeypatch.setattr(AnthropicProvider, "__init__", _fake_init)
    KimiForCodingProvider()
    assert captured["max_tokens"] == 8096


def test_kimi_max_tokens_explicit(monkeypatch):
    monkeypatch.setenv("KIMI_FOR_CODING_API_KEY", "k")
    captured = {}

    def _fake_init(self, *, api_key=None, max_tokens=8096, base_url=None, default_headers=None):
        captured["max_tokens"] = max_tokens

    monkeypatch.setattr(AnthropicProvider, "__init__", _fake_init)
    KimiForCodingProvider(max_tokens=16384)
    assert captured["max_tokens"] == 16384


# ── 4. Class-level feature flags ──────────────────────────────────────────────


def test_kimi_supports_thinking():
    assert KimiForCodingProvider._supports_thinking is True


def test_kimi_thinking_uses_betas_is_false():
    assert KimiForCodingProvider._thinking_uses_betas is False


def test_kimi_does_not_support_cache_control():
    assert KimiForCodingProvider._supports_cache_control is False


# ── 5. complete() thinking behavior ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_kimi_thinking_enabled_uses_extra_body_not_betas():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
        thinking=True,
        thinking_budget=5000,
    )

    call = captured[0]
    assert "betas" not in call
    assert "thinking" not in call
    assert call.get("extra_body") == {"thinking": {"type": "enabled"}}
    assert call["max_tokens"] == max(8096, 5000 + 1000)


@pytest.mark.asyncio
async def test_kimi_thinking_disabled_omits_extra_body():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
        thinking=False,
    )

    call = captured[0]
    assert "betas" not in call
    assert "thinking" not in call
    assert "extra_body" not in call
    assert call["max_tokens"] == 8096


@pytest.mark.asyncio
async def test_kimi_thinking_stream_routes_to_regular_messages():
    provider = _make_provider()

    events = [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="hello "),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="world"),
        ),
    ]
    final_message = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hello world")],
        usage=SimpleNamespace(
            input_tokens=2,
            output_tokens=2,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )

    provider._client = SimpleNamespace(
        messages=SimpleNamespace(
            stream=lambda **kwargs: _FakeStream(events, final_message),
        )
    )

    chunks: list[str] = []
    text, tool_calls, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
        thinking=True,
        thinking_budget=4000,
        on_text_chunk=chunks.append,
    )

    assert chunks == ["hello ", "world"]
    assert text == "hello world"
    assert tool_calls == []
    assert usage.input_tokens == 2
    assert usage.output_tokens == 2


@pytest.mark.asyncio
async def test_kimi_thinking_stream_routes_to_thinking_hooks_not_text_chunks():
    """v2.0.9: when stream lacks thinking_delta, final message thinking is
    delivered to on_thinking_start / on_thinking_end — never to on_text_chunk.
    """
    provider = _make_provider()

    events = [
        SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="text")),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="answer"),
        ),
        SimpleNamespace(type="content_block_stop"),
    ]
    final_message = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="reasoning..."),
            SimpleNamespace(type="text", text="answer"),
        ],
        usage=SimpleNamespace(
            input_tokens=3,
            output_tokens=3,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )

    provider._client = SimpleNamespace(
        messages=SimpleNamespace(
            stream=lambda **kwargs: _FakeStream(events, final_message),
        )
    )

    chunks: list[str] = []
    thinking_starts: list[None] = []
    thinking_bodies: list[str] = []
    text, tool_calls, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
        thinking=True,
        on_text_chunk=chunks.append,
        on_thinking_start=lambda: thinking_starts.append(None),
        on_thinking_end=thinking_bodies.append,
    )

    assert chunks == ["answer"]
    assert len(thinking_starts) == 1
    assert thinking_bodies == ["reasoning..."]
    assert text == "answer"
    assert tool_calls == []


# ── 6. System prompt / cache prefix behavior ──────────────────────────────────


@pytest.mark.asyncio
async def test_kimi_system_prompt_plain_string():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="You are helpful.",
        model="kimi-k2",
    )

    assert captured[0]["system"] == "You are helpful."


@pytest.mark.asyncio
async def test_kimi_cache_prefix_concatenated_not_block_list():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="dynamic",
        model="kimi-k2",
        cache_system_prefix="static",
    )

    system = captured[0]["system"]
    assert isinstance(system, str)
    assert system == "static\ndynamic"


@pytest.mark.asyncio
async def test_kimi_cache_prefix_empty_dynamic():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="",
        model="kimi-k2",
        cache_system_prefix="static only",
    )

    assert captured[0]["system"] == "static only"


# ── 7. Message building ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kimi_messages_plain():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_client(captured)

    await provider.complete(
        messages=[
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi there"),
        ],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
    )

    api_msgs = captured[0]["messages"]
    assert api_msgs[0] == {"role": "user", "content": "hello"}
    assert api_msgs[1] == {"role": "assistant", "content": "hi there"}


@pytest.mark.asyncio
async def test_kimi_messages_no_cache_control_even_when_requested():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_client(captured)

    await provider.complete(
        messages=[
            Message(role="user", content="first"),
            Message(role="assistant", content="reply"),
            Message(role="user", content="second"),
        ],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
        cache_last_human_turn=True,
    )

    api_msgs = captured[0]["messages"]
    for msg in api_msgs:
        assert isinstance(msg["content"], str)
        assert "cache_control" not in msg


@pytest.mark.asyncio
async def test_kimi_messages_list_content_preserved_for_assistant():
    """Assistant messages with list content are converted to API format."""
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_client(captured)

    await provider.complete(
        messages=[
            Message(role="assistant", content=[
                {"type": "text", "text": "part1"},
                {"type": "text", "text": "part2"},
            ]),
        ],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
    )

    api_msgs = captured[0]["messages"]
    assert api_msgs[0]["role"] == "assistant"
    assert api_msgs[0]["content"] == [
        {"type": "text", "text": "part1"},
        {"type": "text", "text": "part2"},
    ]


# ── 8. Tool handling ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kimi_tools_passed_to_api():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="search")],
        tools=[_dummy_tool()],
        system_prompt="sys",
        model="kimi-k2",
    )

    assert "tools" in captured[0]
    tools = captured[0]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "search"


@pytest.mark.asyncio
async def test_kimi_no_tools_key_when_empty():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
    )

    assert "tools" not in captured[0]


@pytest.mark.asyncio
async def test_kimi_collects_tool_calls_from_response():
    provider = _make_provider()

    async def _create(**kwargs):
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text=""),
                SimpleNamespace(type="tool_use", id="tool-1", name="search", input={"q": "abc"}),
            ],
            usage=SimpleNamespace(
                input_tokens=5,
                output_tokens=3,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )

    provider._client = SimpleNamespace(messages=SimpleNamespace(create=_create))

    text, tool_calls, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
    )

    assert text == ""
    assert len(tool_calls) == 1
    assert tool_calls[0].id == "tool-1"
    assert tool_calls[0].name == "search"
    assert tool_calls[0].input == {"q": "abc"}


@pytest.mark.asyncio
async def test_kimi_collects_multiple_tool_calls_from_response():
    provider = _make_provider()

    async def _create(**kwargs):
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text=""),
                SimpleNamespace(type="tool_use", id="t1", name="bash", input={"cmd": "ls"}),
                SimpleNamespace(type="tool_use", id="t2", name="search", input={"q": "x"}),
            ],
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=6,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )

    provider._client = SimpleNamespace(messages=SimpleNamespace(create=_create))

    text, tool_calls, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
    )

    assert text == ""
    assert len(tool_calls) == 2
    assert tool_calls[0].name == "bash"
    assert tool_calls[1].name == "search"
    assert usage.total_tokens == 16


# ── 9. Usage extraction ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kimi_usage_extraction_standard():
    provider = _make_provider()

    async def _create(**kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=SimpleNamespace(
                input_tokens=50,
                output_tokens=20,
                cache_read_input_tokens=10,
                cache_creation_input_tokens=5,
            ),
        )

    provider._client = SimpleNamespace(messages=SimpleNamespace(create=_create))

    _, _, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
    )

    assert isinstance(usage, TokenUsage)
    assert usage.input_tokens == 50
    assert usage.output_tokens == 20
    assert usage.cache_read_tokens == 10
    assert usage.cache_write_tokens == 5
    assert usage.total_tokens == 70


@pytest.mark.asyncio
async def test_kimi_usage_extraction_with_reasoning_tokens():
    """Kimi For Coding may return reasoning_tokens in usage; we must surface them."""
    provider = _make_provider()

    async def _create(**kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=SimpleNamespace(
                input_tokens=50,
                output_tokens=20,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                output_tokens_details=SimpleNamespace(reasoning_tokens=15),
            ),
        )

    provider._client = SimpleNamespace(messages=SimpleNamespace(create=_create))

    _, _, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
    )

    assert usage.reasoning_tokens == 15


@pytest.mark.asyncio
async def test_kimi_usage_extraction_missing_fields_returns_zeros():
    provider = _make_provider()

    async def _create(**kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=SimpleNamespace(),
        )

    provider._client = SimpleNamespace(messages=SimpleNamespace(create=_create))

    _, _, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
    )

    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cache_read_tokens == 0
    assert usage.cache_write_tokens == 0
    assert usage.reasoning_tokens == 0


@pytest.mark.asyncio
async def test_kimi_usage_extraction_no_usage_returns_empty():
    provider = _make_provider()

    async def _create(**kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=None,
        )

    provider._client = SimpleNamespace(messages=SimpleNamespace(create=_create))

    _, _, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
    )

    assert usage == TokenUsage()


# ── 10. Model param passed through ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kimi_model_param_passed_to_api():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2-0711-preview",
    )

    assert captured[0]["model"] == "kimi-k2-0711-preview"


@pytest.mark.asyncio
async def test_kimi_model_param_arbitrary_string():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="moonshot-kimi-coder",
    )

    assert captured[0]["model"] == "moonshot-kimi-coder"


# ── 11. Regression: Kimi does NOT have OpenAI _tc_map_to_list empty-name bug ──


def test_kimi_does_not_use_tc_map_to_list():
    """Kimi inherits Anthropic-style response parsing, not OpenAI streaming tc_map."""
    from butterfly.llm_engine.providers.openai_api import _tc_map_to_list

    # Kimi provider code path never touches _tc_map_to_list because it uses
    # Anthropic's message-format responses where tool_use blocks are complete.
    tc_map = {
        0: {"id": "tc1", "name": "", "arguments": "{}"},
    }
    # Post-fix (Bug 11): _tc_map_to_list filters empty-name entries, so
    # the OpenAI helper now produces an empty list from this map.
    openai_result = _tc_map_to_list(tc_map)
    assert openai_result == []

    # Kimi's Anthropic-based path does not accumulate fragments in a map;
    # tool_use blocks arrive whole from the API response.
    # This test documents that Kimi is NOT affected by the empty-name bug.
    assert KimiForCodingProvider.__mro__[1].__name__ == "AnthropicProvider"
