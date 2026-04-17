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
