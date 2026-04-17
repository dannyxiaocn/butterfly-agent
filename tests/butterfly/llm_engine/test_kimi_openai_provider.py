"""Unit tests for ``KimiOpenAIProvider``.

``KimiOpenAIProvider`` subclasses ``OpenAIProvider`` and talks to Moonshot's
OpenAI-compatible surface (``/coding/v1/chat/completions``). The contract it
adds on top of the base class:

- Resolve ``api_key`` from ``KIMI_FOR_CODING_API_KEY`` **only** (legacy
  ``KIMI_API_KEY`` is not accepted), failing fast when the env var is not
  set and no explicit ``api_key`` was passed.
- Hardcode ``base_url`` to ``https://api.kimi.com/coding/v1/`` — no env
  override, no constructor parameter.
- Inject ``extra_body={"thinking": {"type": "enabled"}}`` when
  ``thinking=True``; omit the key otherwise.
- Extract ``cached_tokens`` from Moonshot's top-level usage attribute when
  the standard ``prompt_tokens_details.cached_tokens`` is absent.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from butterfly.core.types import Message, TokenUsage
from butterfly.llm_engine.errors import AuthError
from butterfly.llm_engine.providers.kimi import (
    KimiOpenAIProvider,
    _KIMI_OPENAI_BASE_URL,
)
from butterfly.llm_engine.providers.openai_api import OpenAIProvider


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_provider() -> KimiOpenAIProvider:
    """Construct a KimiOpenAIProvider without calling the real OpenAI SDK."""
    p = KimiOpenAIProvider.__new__(KimiOpenAIProvider)
    p.max_tokens = 8096
    p._client = None  # patched per-test
    return p


def _fake_chat_client(captured: list) -> SimpleNamespace:
    """Return a fake chat.completions.create that captures kwargs."""
    async def _create(**kwargs: Any) -> SimpleNamespace:
        captured.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=None)
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=3,
                prompt_tokens_details=None,
                completion_tokens_details=None,
            ),
        )

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )


# ── 1. Constructor: base_url is hardcoded ─────────────────────────────────────


def test_kimi_openai_base_url_is_hardcoded(monkeypatch):
    """``_KIMI_OPENAI_BASE_URL`` is always passed through — no override exists."""
    # A stale env var should have no effect (the provider does not read one).
    monkeypatch.setenv("KIMI_OPENAI_BASE_URL", "https://ignored.kimi.com/v1/")
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, base_url=None, max_tokens=8096, max_retries=3, default_headers=None):
        captured["api_key"] = api_key
        captured["base_url"] = base_url

    monkeypatch.setattr(OpenAIProvider, "__init__", _fake_init)
    KimiOpenAIProvider(api_key="k")
    assert captured["base_url"] == _KIMI_OPENAI_BASE_URL


def test_kimi_openai_ctor_rejects_base_url_kwarg(monkeypatch):
    """The ``base_url`` constructor parameter was removed — passing it errors."""
    monkeypatch.setenv("KIMI_FOR_CODING_API_KEY", "k")
    with pytest.raises(TypeError):
        KimiOpenAIProvider(base_url="https://explicit.kimi.com/v1/")  # type: ignore[call-arg]


# ── 2. Constructor: API key resolution ────────────────────────────────────────


def test_kimi_openai_fails_fast_without_any_key(monkeypatch):
    monkeypatch.delenv("KIMI_FOR_CODING_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)

    with pytest.raises(AuthError) as exc_info:
        KimiOpenAIProvider()
    assert "KIMI_FOR_CODING_API_KEY" in str(exc_info.value)


def test_kimi_openai_primary_env_key(monkeypatch):
    monkeypatch.setenv("KIMI_FOR_CODING_API_KEY", "primary")
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, base_url=None, max_tokens=8096, max_retries=3, default_headers=None):
        captured["api_key"] = api_key

    monkeypatch.setattr(OpenAIProvider, "__init__", _fake_init)
    KimiOpenAIProvider()
    assert captured["api_key"] == "primary"


def test_kimi_openai_legacy_kimi_api_key_is_ignored(monkeypatch):
    """Only KIMI_FOR_CODING_API_KEY is honored; legacy KIMI_API_KEY is not."""
    monkeypatch.delenv("KIMI_FOR_CODING_API_KEY", raising=False)
    monkeypatch.setenv("KIMI_API_KEY", "legacy")

    with pytest.raises(AuthError):
        KimiOpenAIProvider()


def test_kimi_openai_explicit_key_overrides_env(monkeypatch):
    monkeypatch.setenv("KIMI_FOR_CODING_API_KEY", "env-key")
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, base_url=None, max_tokens=8096, max_retries=3, default_headers=None):
        captured["api_key"] = api_key

    monkeypatch.setattr(OpenAIProvider, "__init__", _fake_init)
    KimiOpenAIProvider(api_key="explicit")
    assert captured["api_key"] == "explicit"


# ── 3. Thinking behavior ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kimi_openai_thinking_enabled_injects_extra_body():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_chat_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
        thinking=True,
    )

    call = captured[0]
    assert call.get("extra_body") == {"thinking": {"type": "enabled"}}
    # Non-reasoning model, so the base class keeps `max_tokens` as-is.
    assert call["max_tokens"] == 8096


@pytest.mark.asyncio
async def test_kimi_openai_thinking_disabled_omits_extra_body():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_chat_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
    )

    call = captured[0]
    assert "extra_body" not in call


# ── 4. Usage extraction ───────────────────────────────────────────────────────


def test_kimi_usage_prefers_prompt_tokens_details_when_present():
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=40,
        prompt_tokens_details=SimpleNamespace(cached_tokens=30),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=5),
    )
    out = KimiOpenAIProvider._extract_usage(usage)
    assert out == TokenUsage(
        input_tokens=70,  # 100 - 30 cached
        output_tokens=40,
        cache_read_tokens=30,
        cache_write_tokens=0,
        reasoning_tokens=5,
    )


def test_kimi_usage_falls_back_to_top_level_cached_tokens():
    """Moonshot-style: no prompt_tokens_details, cached_tokens on usage root."""
    usage = SimpleNamespace(
        prompt_tokens=80,
        completion_tokens=20,
        cached_tokens=25,
        prompt_tokens_details=None,
        completion_tokens_details=None,
    )
    out = KimiOpenAIProvider._extract_usage(usage)
    assert out.cache_read_tokens == 25
    assert out.input_tokens == 55  # 80 - 25
    assert out.output_tokens == 20


def test_kimi_usage_no_cache_at_all():
    usage = SimpleNamespace(
        prompt_tokens=50,
        completion_tokens=10,
        prompt_tokens_details=None,
        completion_tokens_details=None,
    )
    out = KimiOpenAIProvider._extract_usage(usage)
    assert out == TokenUsage(input_tokens=50, output_tokens=10)


def test_kimi_usage_prompt_details_wins_over_top_level():
    """If both shapes are populated, the standard field takes precedence."""
    usage = SimpleNamespace(
        prompt_tokens=200,
        completion_tokens=40,
        cached_tokens=99,  # should be ignored since prompt_tokens_details.cached_tokens is set
        prompt_tokens_details=SimpleNamespace(cached_tokens=60),
        completion_tokens_details=None,
    )
    out = KimiOpenAIProvider._extract_usage(usage)
    assert out.cache_read_tokens == 60
    assert out.input_tokens == 140  # 200 - 60


@pytest.mark.asyncio
async def test_kimi_openai_complete_returns_usage_via_override():
    """End-to-end: non-streaming path threads usage through the subclass hook."""
    provider = _make_provider()

    async def _create(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="hi", tool_calls=None))
            ],
            usage=SimpleNamespace(
                prompt_tokens=120,
                completion_tokens=12,
                cached_tokens=20,
                prompt_tokens_details=None,
                completion_tokens_details=None,
            ),
        )

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )

    text, tool_calls, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
    )
    assert text == "hi"
    assert tool_calls == []
    assert usage.cache_read_tokens == 20
    assert usage.input_tokens == 100
    assert usage.output_tokens == 12


# ── 5. Streaming usage (chunk-on-choice fallback) ────────────────────────────


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def _gen():
            for c in self._chunks:
                yield c
        return _gen()


@pytest.mark.asyncio
async def test_kimi_openai_streaming_reads_usage_from_chunk_top_level():
    """Standard path: usage on ``chunk.usage``."""
    provider = _make_provider()

    chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="hi", tool_calls=None),
                    index=0,
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(
                prompt_tokens=70,
                completion_tokens=5,
                cached_tokens=20,
                prompt_tokens_details=None,
                completion_tokens_details=None,
            ),
        ),
    ]

    async def _create(**kwargs: Any):
        return _FakeStream(chunks)

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )

    out: list[str] = []
    text, tool_calls, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
        on_text_chunk=out.append,
    )
    assert text == "hi"
    assert tool_calls == []
    assert usage.cache_read_tokens == 20
    assert usage.input_tokens == 50


@pytest.mark.asyncio
async def test_kimi_openai_streaming_reads_usage_from_choice_when_chunk_usage_absent():
    """Moonshot variant: no chunk.usage but choices[0].usage carries it."""
    provider = _make_provider()

    chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="ok", tool_calls=None),
                    index=0,
                    usage=SimpleNamespace(
                        prompt_tokens=40,
                        completion_tokens=2,
                        cached_tokens=10,
                        prompt_tokens_details=None,
                        completion_tokens_details=None,
                    ),
                )
            ],
            usage=None,
        ),
    ]

    async def _create(**kwargs: Any):
        return _FakeStream(chunks)

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )

    out: list[str] = []
    text, tool_calls, usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
        on_text_chunk=out.append,
    )
    assert text == "ok"
    assert usage.cache_read_tokens == 10
    assert usage.input_tokens == 30


# ── 6. Registry wiring ────────────────────────────────────────────────────────


def test_registry_default_kimi_is_openai_variant(monkeypatch):
    """``kimi-coding-plan`` must now resolve to ``KimiOpenAIProvider``."""
    monkeypatch.setenv("KIMI_FOR_CODING_API_KEY", "fake")
    from butterfly.llm_engine.registry import resolve_provider

    p = resolve_provider("kimi-coding-plan")
    assert isinstance(p, KimiOpenAIProvider)


def test_registry_anthropic_variant_reachable(monkeypatch):
    """``kimi-coding-plan-anthropic`` must resolve to ``KimiAnthropicProvider``."""
    monkeypatch.setenv("KIMI_FOR_CODING_API_KEY", "fake")
    from butterfly.llm_engine.providers.kimi import KimiAnthropicProvider
    from butterfly.llm_engine.registry import resolve_provider

    p = resolve_provider("kimi-coding-plan-anthropic")
    assert isinstance(p, KimiAnthropicProvider)


def test_back_compat_alias_is_anthropic_variant():
    from butterfly.llm_engine.providers.kimi import (
        KimiAnthropicProvider,
        KimiForCodingProvider,
    )

    assert KimiForCodingProvider is KimiAnthropicProvider


# ── User-Agent header (required by Kimi For Coding access control) ────────────


def test_kimi_openai_passes_user_agent_header(monkeypatch):
    """Both Kimi providers must pass User-Agent: claude-code/0.1.0 to the SDK."""
    from butterfly.llm_engine.providers.kimi import (
        _KIMI_USER_AGENT,
        _KIMI_DEFAULT_HEADERS,
        KimiOpenAIProvider,
    )
    from butterfly.llm_engine.providers.openai_api import OpenAIProvider

    captured: dict = {}

    def _fake_init(self, *, api_key=None, base_url=None, max_tokens=8096,
                   max_retries=3, default_headers=None):
        captured["default_headers"] = default_headers

    monkeypatch.setattr(OpenAIProvider, "__init__", _fake_init)
    KimiOpenAIProvider(api_key="k")

    assert captured["default_headers"] == _KIMI_DEFAULT_HEADERS
    assert captured["default_headers"]["User-Agent"] == _KIMI_USER_AGENT
    assert _KIMI_USER_AGENT == "claude-code/0.1.0"


def test_kimi_anthropic_passes_user_agent_header(monkeypatch):
    from butterfly.llm_engine.providers.kimi import (
        _KIMI_USER_AGENT,
        _KIMI_DEFAULT_HEADERS,
        KimiAnthropicProvider,
    )
    from butterfly.llm_engine.providers.anthropic import AnthropicProvider

    captured: dict = {}

    def _fake_init(self, *, api_key=None, max_tokens=8096, base_url=None,
                   default_headers=None):
        captured["default_headers"] = default_headers

    monkeypatch.setattr(AnthropicProvider, "__init__", _fake_init)
    KimiAnthropicProvider(api_key="k")

    assert captured["default_headers"] == _KIMI_DEFAULT_HEADERS
    assert captured["default_headers"]["User-Agent"] == _KIMI_USER_AGENT


# ── 7. reasoning_content round-trip (v2.0.16 fix) ─────────────────────────────
#
# Moonshot/Kimi streams reasoning tokens as ``delta.reasoning_content`` and
# expects every assistant message carrying ``tool_calls`` on subsequent
# requests to include a matching ``reasoning_content`` string. Losing that
# field between turns causes Kimi to 400 with
# "thinking is enabled but reasoning_content is missing" — which used to
# crash Agent.run the moment it tried the second iteration after a tool
# call. These tests lock down the end-to-end round-trip.


def _fake_stream_client(chunks: list) -> SimpleNamespace:
    """Fake ``chat.completions.create`` that yields provided streaming chunks."""

    async def _create(**kwargs: Any) -> Any:
        async def _gen():
            for c in chunks:
                yield c
        return _gen()

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )


def _chunk(
    *,
    content: str | None = None,
    reasoning: str | None = None,
    usage: Any = None,
) -> SimpleNamespace:
    delta = SimpleNamespace(content=content, tool_calls=None, reasoning_content=reasoning)
    choice = SimpleNamespace(delta=delta)
    return SimpleNamespace(choices=[choice], usage=usage)


def _usage_chunk(usage: Any) -> SimpleNamespace:
    return SimpleNamespace(choices=[], usage=usage)


@pytest.mark.asyncio
async def test_stream_captures_reasoning_content_into_pending_slot():
    """``delta.reasoning_content`` accumulates into ``_pending_reasoning_content``."""
    provider = _make_provider()
    chunks = [
        _chunk(reasoning="Let me "),
        _chunk(reasoning="think about it."),
        _chunk(content="Hi!"),
        _usage_chunk(SimpleNamespace(
            prompt_tokens=5, completion_tokens=2,
            prompt_tokens_details=None, completion_tokens_details=None,
        )),
    ]
    provider._client = _fake_stream_client(chunks)

    text, tool_calls, _usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2-turbo-preview",
        thinking=True,
        on_text_chunk=lambda _: None,
    )

    assert text == "Hi!"
    assert tool_calls == []
    assert provider._pending_reasoning_content == "Let me think about it."


@pytest.mark.asyncio
async def test_stream_fires_thinking_hooks_on_reasoning_then_content():
    """Reasoning opens the thinking cell; the first content delta closes it."""
    provider = _make_provider()
    chunks = [
        _chunk(reasoning="plan"),
        _chunk(reasoning=" step"),
        _chunk(content="done"),
    ]
    provider._client = _fake_stream_client(chunks)

    events: list[tuple[str, str]] = []

    await provider.complete(
        messages=[Message(role="user", content="x")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
        thinking=True,
        on_text_chunk=lambda _: None,
        on_thinking_start=lambda: events.append(("start", "")),
        on_thinking_end=lambda body: events.append(("end", body)),
    )

    assert events[0] == ("start", "")
    assert events[-1] == ("end", "plan step")
    # End must fire exactly once even when more content deltas follow.
    assert sum(1 for e in events if e[0] == "end") == 1


@pytest.mark.asyncio
async def test_stream_fires_thinking_end_on_reasoning_only_stream():
    """Stream that ends with only reasoning (no assistant text, no tool call)
    still closes the thinking cell so the UI does not leak a "thinking…" pill.
    """
    provider = _make_provider()
    chunks = [_chunk(reasoning="mid-thought")]
    provider._client = _fake_stream_client(chunks)

    events: list[tuple[str, str]] = []

    await provider.complete(
        messages=[Message(role="user", content="x")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
        thinking=True,
        on_text_chunk=lambda _: None,
        on_thinking_start=lambda: events.append(("start", "")),
        on_thinking_end=lambda body: events.append(("end", body)),
    )

    assert ("end", "mid-thought") in events


@pytest.mark.asyncio
async def test_stream_without_reasoning_leaves_pending_slot_empty():
    """Standard OpenAI streams never populate reasoning_content — the pending
    slot stays empty so ``consume_extra_blocks`` returns []."""
    provider = _make_provider()
    chunks = [_chunk(content="hello")]
    provider._client = _fake_stream_client(chunks)

    await provider.complete(
        messages=[Message(role="user", content="x")],
        tools=[],
        system_prompt="sys",
        model="gpt-4",
        on_text_chunk=lambda _: None,
    )

    assert provider._pending_reasoning_content == ""
    assert provider.consume_extra_blocks() == []


def test_consume_extra_blocks_returns_reasoning_and_clears_slot():
    """``consume_extra_blocks`` drains the pending reasoning and clears it."""
    provider = _make_provider()
    provider._pending_reasoning_content = "captured reasoning"

    first = provider.consume_extra_blocks()
    assert first == [{"type": "reasoning_content", "text": "captured reasoning"}]
    # A second call returns [] — the block was consumed.
    assert provider.consume_extra_blocks() == []


@pytest.mark.asyncio
async def test_non_stream_captures_message_reasoning_content():
    """Non-streaming responses capture ``message.reasoning_content`` too."""
    provider = _make_provider()

    async def _create(**kwargs: Any) -> Any:
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content="Hi", tool_calls=None,
                    reasoning_content="thought through it",
                ),
            )],
            usage=SimpleNamespace(
                prompt_tokens=3, completion_tokens=1,
                prompt_tokens_details=None, completion_tokens_details=None,
            ),
        )

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )

    text, _tool_calls, _usage = await provider.complete(
        messages=[Message(role="user", content="x")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
        thinking=True,
    )

    assert text == "Hi"
    assert provider._pending_reasoning_content == "thought through it"


def test_build_messages_stamps_reasoning_content_on_tool_call_turn():
    """Assistant turn with both reasoning_content and tool_use → entry carries
    the ``reasoning_content`` field Kimi's API demands on iter 2+."""
    from butterfly.llm_engine.providers.openai_api import _build_messages

    messages = [
        Message(role="user", content="read my config"),
        Message(role="assistant", content=[
            {"type": "reasoning_content", "text": "need to call read"},
            {"type": "tool_use", "id": "tu1", "name": "read",
             "input": {"path": "config.yaml"}},
        ]),
        Message(role="tool", content=[
            {"type": "tool_result", "tool_use_id": "tu1",
             "content": "model: kimi", "is_error": False},
        ]),
    ]

    out = _build_messages("sys", messages)
    # system, user, assistant(tool_call), tool
    assert out[2]["role"] == "assistant"
    assert out[2]["tool_calls"][0]["function"]["name"] == "read"
    assert out[2]["reasoning_content"] == "need to call read"


def test_build_messages_does_not_stamp_reasoning_on_text_only_turn():
    """Text-only assistant turns must NOT carry ``reasoning_content`` so a
    mixed history does not 400 standard OpenAI models that reject the field.
    """
    from butterfly.llm_engine.providers.openai_api import _build_messages

    messages = [
        Message(role="user", content="hi"),
        Message(role="assistant", content=[
            {"type": "reasoning_content", "text": "thinking..."},
            {"type": "text", "text": "Hello!"},
        ]),
    ]

    out = _build_messages("sys", messages)
    assistant_entry = out[2]
    assert assistant_entry["content"] == "Hello!"
    assert "reasoning_content" not in assistant_entry
    assert "tool_calls" not in assistant_entry


def test_session_clean_content_preserves_reasoning_content():
    """``Session._clean_content_for_api`` must allow-list ``reasoning_content``
    so a session reload still carries the field Kimi requires on the next
    LLM call."""
    from butterfly.session_engine.session import Session

    content = [
        {"type": "reasoning_content", "text": "captured"},
        {"type": "tool_use", "id": "tu1", "name": "read",
         "input": {"path": "x"}},
    ]
    cleaned = Session._clean_content_for_api(content)

    assert {"type": "reasoning_content", "text": "captured"} in cleaned
    # And storage-only fields on reasoning blocks (if any future writer added
    # one) get stripped — only ``type`` + ``text`` survive.
    enriched = [{"type": "reasoning_content", "text": "x", "ts": "should-strip"}]
    stripped = Session._clean_content_for_api(enriched)
    assert stripped == [{"type": "reasoning_content", "text": "x"}]


@pytest.mark.asyncio
async def test_agent_loop_carries_reasoning_across_iterations():
    """End-to-end: Agent.run should pull reasoning_content out via
    ``consume_extra_blocks`` after iter 1 and attach it to the assistant
    message so iter 2's request already contains it — the regression that
    previously 400'd Kimi on every tool-call follow-up.
    """
    from butterfly.core.agent import Agent
    from butterfly.core.tool import Tool
    from butterfly.core.types import ToolCall, TokenUsage

    calls: list[list[Message]] = []

    class _StubProvider:
        _supports_cache_control = False

        def __init__(self) -> None:
            self._pending = "my reasoning"

        def consume_extra_blocks(self) -> list[dict]:
            if not self._pending:
                return []
            out = [{"type": "reasoning_content", "text": self._pending}]
            self._pending = ""
            return out

        async def complete(self, *, messages, tools, system_prompt, model, **_kw):
            calls.append(list(messages))
            if len(calls) == 1:
                # iter 1: fire a tool call
                return "", [ToolCall(id="tu1", name="read", input={"path": "x"})], TokenUsage()
            # iter 2: produce final text
            return "done", [], TokenUsage()

    async def _read(path: str) -> str:
        return f"contents of {path}"

    tool = Tool(
        name="read", description="Read a file.", func=_read,
        schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    agent = Agent(
        system_prompt="sys", tools=[tool], model="stub",
        provider=_StubProvider(),
    )

    result = await agent.run("go")

    assert result.iterations == 2
    assert result.content == "done"

    # The second LLM call must have seen the reasoning_content block inside
    # the assistant message — that is what round-trips to Kimi as the
    # ``reasoning_content`` field on the request body.
    iter2_messages = calls[1]
    assistant_turn = iter2_messages[1]
    assert assistant_turn.role == "assistant"
    assert isinstance(assistant_turn.content, list)
    types = [b.get("type") for b in assistant_turn.content if isinstance(b, dict)]
    assert "reasoning_content" in types
    assert "tool_use" in types


# ── 8. Correctness invariants around the pending slot ────────────────────────


@pytest.mark.asyncio
async def test_stream_interleaved_reasoning_after_content_still_captured():
    """If reasoning arrives AFTER content (interleaved), the pending slot must
    still capture the full reasoning body so Kimi's tool-loop round-trip is
    preserved. The thinking cell closes on the first content chunk (one-shot),
    but that is a UI concern — the API field the provider echoes back must be
    complete regardless of arrival order.
    """
    provider = _make_provider()
    chunks = [
        _chunk(reasoning="pre-"),
        _chunk(content="hi"),
        _chunk(reasoning="post"),
    ]
    provider._client = _fake_stream_client(chunks)

    events: list[tuple[str, str]] = []
    await provider.complete(
        messages=[Message(role="user", content="x")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2",
        thinking=True,
        on_text_chunk=lambda _: None,
        on_thinking_start=lambda: events.append(("start", "")),
        on_thinking_end=lambda body: events.append(("end", body)),
    )

    # Full reasoning text round-trips through pending — this is what gets
    # echoed back to Kimi on the next request body.
    assert provider._pending_reasoning_content == "pre-post"
    # UI cell closes exactly once (on the first content delta).
    assert sum(1 for e in events if e[0] == "end") == 1


@pytest.mark.asyncio
async def test_stream_clears_pending_when_no_reasoning_in_current_call():
    """Consecutive calls on the same provider: a call with reasoning populates
    the slot; a subsequent call without reasoning must clear it so the stale
    value does not round-trip to an unrelated turn.
    """
    provider = _make_provider()

    # Call 1: reasoning present.
    provider._client = _fake_stream_client([
        _chunk(reasoning="stale"), _chunk(content="a"),
    ])
    await provider.complete(
        messages=[Message(role="user", content="x")],
        tools=[], system_prompt="sys", model="kimi-k2",
        thinking=True, on_text_chunk=lambda _: None,
    )
    assert provider._pending_reasoning_content == "stale"

    # Call 2: no reasoning chunks at all. Pending must be cleared so
    # consume_extra_blocks returns [] for callers that did not drain
    # after call 1 (defensive — Agent.run normally drains immediately).
    provider._client = _fake_stream_client([_chunk(content="b")])
    await provider.complete(
        messages=[Message(role="user", content="x")],
        tools=[], system_prompt="sys", model="kimi-k2",
        on_text_chunk=lambda _: None,
    )
    assert provider._pending_reasoning_content == ""
    assert provider.consume_extra_blocks() == []


def test_build_messages_keeps_last_reasoning_when_multiple_blocks_present():
    """Defensive: if an assistant message somehow carries multiple
    ``reasoning_content`` blocks (future-proofing against a provider that
    emits per-step reasoning), ``_build_messages`` stamps only the last one.
    Kimi's API expects a single string, so collapsing to "last wins" matches
    the in-code documentation.
    """
    from butterfly.llm_engine.providers.openai_api import _build_messages

    messages = [
        Message(role="user", content="do it"),
        Message(role="assistant", content=[
            {"type": "reasoning_content", "text": "first"},
            {"type": "reasoning_content", "text": "second"},
            {"type": "tool_use", "id": "tu1", "name": "read",
             "input": {"path": "x"}},
        ]),
    ]
    out = _build_messages("sys", messages)
    assert out[2]["reasoning_content"] == "second"


def test_build_messages_skips_reasoning_when_no_tool_calls_even_if_text_present():
    """A mixed history replayed against a standard OpenAI model (fallback
    scenario) must not leak ``reasoning_content`` onto text-only assistant
    entries — OpenAI rejects the field on plain-text turns. Covered by
    ``test_build_messages_does_not_stamp_reasoning_on_text_only_turn`` for
    the single-block case; this extends that to a mixed-blocks turn with
    both text and reasoning (no tool_use).
    """
    from butterfly.llm_engine.providers.openai_api import _build_messages

    messages = [
        Message(role="user", content="hi"),
        Message(role="assistant", content=[
            {"type": "reasoning_content", "text": "pondering"},
            {"type": "text", "text": "hello"},
        ]),
    ]
    out = _build_messages("sys", messages)
    assistant_entry = out[2]
    assert assistant_entry["content"] == "hello"
    assert "reasoning_content" not in assistant_entry
    assert "tool_calls" not in assistant_entry
