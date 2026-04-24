"""Unit tests for ``DeepSeekProvider``.

``DeepSeekProvider`` subclasses ``OpenAIProvider`` and talks to DeepSeek's
OpenAI-compatible Chat Completions API. The contract it adds on top of the
base class:

- Resolve ``api_key`` from ``DEEPSEEK_API_KEY`` (explicit kwarg wins), failing
  fast with ``AuthError`` when neither is provided.
- Base URL defaults to ``https://api.deepseek.com`` with an optional
  ``DEEPSEEK_BASE_URL`` override for gateways / self-hosted deployments.
- Inject ``extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": ...}``
  when ``thinking=True``; omit the key otherwise. Unknown effort strings get
  mapped onto DeepSeek's two-level ``high``/``max`` scale.
- Extract cache tokens from DeepSeek's top-level ``prompt_cache_hit_tokens``
  when the standard ``prompt_tokens_details.cached_tokens`` is absent.

Scope — from founder's opinion: only the DeepSeek V4 family is supported.
``deepseek-chat`` / ``deepseek-reasoner`` are out of scope, so there are no
reasoner-scrubbing tests in this file (the provider does not ship that code).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from butterfly.core.types import Message, TokenUsage
from butterfly.llm_engine.errors import AuthError
from butterfly.llm_engine.providers.anthropic import AnthropicProvider
from butterfly.llm_engine.providers.deepseek import (
    DeepSeekAnthropicProvider,
    DeepSeekProvider,
    _DEEPSEEK_ANTHROPIC_BASE_URL,
    _DEEPSEEK_BASE_URL,
)
from butterfly.llm_engine.providers.openai_api import OpenAIProvider


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_provider() -> DeepSeekProvider:
    """Construct a DeepSeekProvider without calling the real OpenAI SDK."""
    p = DeepSeekProvider.__new__(DeepSeekProvider)
    p.max_tokens = 8096
    p._client = None  # patched per-test
    p._pending_reasoning_content = ""
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


# ── 1. Constructor: base URL resolution ──────────────────────────────────────


def test_deepseek_default_base_url(monkeypatch):
    """Public endpoint is used when no override is set."""
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, base_url=None, max_tokens=8096,
                   max_retries=3, default_headers=None):
        captured["base_url"] = base_url

    monkeypatch.setattr(OpenAIProvider, "__init__", _fake_init)
    DeepSeekProvider(api_key="k")
    assert captured["base_url"] == _DEEPSEEK_BASE_URL
    assert _DEEPSEEK_BASE_URL == "https://api.deepseek.com"


def test_deepseek_base_url_env_override(monkeypatch):
    """``DEEPSEEK_BASE_URL`` overrides the default — required for gateways."""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://gw.internal.example/ds")
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, base_url=None, max_tokens=8096,
                   max_retries=3, default_headers=None):
        captured["base_url"] = base_url

    monkeypatch.setattr(OpenAIProvider, "__init__", _fake_init)
    DeepSeekProvider(api_key="k")
    assert captured["base_url"] == "https://gw.internal.example/ds"


def test_deepseek_explicit_base_url_beats_env(monkeypatch):
    """Constructor ``base_url`` wins over the env var."""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://env.example")
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, base_url=None, max_tokens=8096,
                   max_retries=3, default_headers=None):
        captured["base_url"] = base_url

    monkeypatch.setattr(OpenAIProvider, "__init__", _fake_init)
    DeepSeekProvider(api_key="k", base_url="https://explicit.example")
    assert captured["base_url"] == "https://explicit.example"


# ── 2. Constructor: API key resolution ────────────────────────────────────────


def test_deepseek_fails_fast_without_any_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(AuthError) as exc_info:
        DeepSeekProvider()
    assert "DEEPSEEK_API_KEY" in str(exc_info.value)
    assert exc_info.value.provider == "deepseek"
    assert exc_info.value.status == 401


def test_deepseek_env_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, base_url=None, max_tokens=8096,
                   max_retries=3, default_headers=None):
        captured["api_key"] = api_key

    monkeypatch.setattr(OpenAIProvider, "__init__", _fake_init)
    DeepSeekProvider()
    assert captured["api_key"] == "env-key"


def test_deepseek_explicit_key_overrides_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, base_url=None, max_tokens=8096,
                   max_retries=3, default_headers=None):
        captured["api_key"] = api_key

    monkeypatch.setattr(OpenAIProvider, "__init__", _fake_init)
    DeepSeekProvider(api_key="explicit")
    assert captured["api_key"] == "explicit"


# ── 3. Thinking behavior ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deepseek_thinking_enabled_injects_extra_body_with_high_default():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_chat_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="deepseek-v4-pro",
        thinking=True,
    )

    call = captured[0]
    assert call.get("extra_body") == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }
    assert call["max_tokens"] == 8096


@pytest.mark.asyncio
async def test_deepseek_thinking_max_effort_passthrough():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_chat_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="deepseek-v4-pro",
        thinking=True,
        thinking_effort="max",
    )

    assert captured[0]["extra_body"]["reasoning_effort"] == "max"


@pytest.mark.asyncio
async def test_deepseek_thinking_maps_medium_to_high():
    """DeepSeek exposes only ``high``/``max`` — the Anthropic ``medium`` hint
    falls back onto the interactive default so the request stays valid.
    """
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_chat_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="deepseek-v4-pro",
        thinking=True,
        thinking_effort="medium",
    )
    assert captured[0]["extra_body"]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_deepseek_thinking_maps_xhigh_to_max():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_chat_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="deepseek-v4-pro",
        thinking=True,
        thinking_effort="xhigh",
    )
    assert captured[0]["extra_body"]["reasoning_effort"] == "max"


@pytest.mark.asyncio
async def test_deepseek_thinking_disabled_omits_extra_body():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_chat_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="deepseek-v4-pro",
    )

    assert "extra_body" not in captured[0]


# ── 4. Usage extraction: DeepSeek-specific prompt_cache_hit_tokens ────────────


def test_deepseek_usage_prefers_prompt_tokens_details_when_present():
    """Standard OpenAI shape wins when DeepSeek emits both."""
    usage = SimpleNamespace(
        prompt_tokens=120,
        completion_tokens=40,
        prompt_tokens_details=SimpleNamespace(cached_tokens=30),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
        prompt_cache_hit_tokens=99,  # ignored — details wins
    )
    out = DeepSeekProvider._extract_usage(usage)
    assert out == TokenUsage(
        input_tokens=90,
        output_tokens=40,
        cache_read_tokens=30,
        cache_write_tokens=0,
        reasoning_tokens=12,
    )


def test_deepseek_usage_falls_back_to_prompt_cache_hit_tokens():
    """DeepSeek-native: top-level ``prompt_cache_hit_tokens`` / ``miss``."""
    usage = SimpleNamespace(
        prompt_tokens=80,
        completion_tokens=20,
        prompt_cache_hit_tokens=25,
        prompt_cache_miss_tokens=55,
        prompt_tokens_details=None,
        completion_tokens_details=None,
    )
    out = DeepSeekProvider._extract_usage(usage)
    assert out.cache_read_tokens == 25
    assert out.input_tokens == 55
    assert out.output_tokens == 20


def test_deepseek_usage_no_cache_at_all():
    usage = SimpleNamespace(
        prompt_tokens=50,
        completion_tokens=10,
        prompt_tokens_details=None,
        completion_tokens_details=None,
    )
    out = DeepSeekProvider._extract_usage(usage)
    assert out == TokenUsage(input_tokens=50, output_tokens=10)


def test_deepseek_usage_surfaces_reasoning_tokens():
    """``completion_tokens_details.reasoning_tokens`` rides through."""
    usage = SimpleNamespace(
        prompt_tokens=50,
        completion_tokens=80,
        prompt_cache_hit_tokens=10,
        prompt_tokens_details=None,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=30),
    )
    out = DeepSeekProvider._extract_usage(usage)
    assert out.reasoning_tokens == 30
    assert out.cache_read_tokens == 10
    assert out.input_tokens == 40


# ── 5. End-to-end: complete threads usage through the subclass hook ───────────


@pytest.mark.asyncio
async def test_deepseek_complete_returns_usage_via_override():
    provider = _make_provider()

    async def _create(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="hi", tool_calls=None))
            ],
            usage=SimpleNamespace(
                prompt_tokens=120,
                completion_tokens=12,
                prompt_cache_hit_tokens=20,
                prompt_cache_miss_tokens=100,
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
        model="deepseek-v4-pro",
    )
    assert text == "hi"
    assert tool_calls == []
    assert usage.cache_read_tokens == 20
    assert usage.input_tokens == 100
    assert usage.output_tokens == 12


# ── 6. V4 tool-call round-trip preserves reasoning_content ────────────────────


@pytest.mark.asyncio
async def test_complete_on_v4_preserves_tools_and_reasoning_roundtrip():
    """V4-pro must retain tools and round-trip reasoning_content (required by
    DeepSeek's thinking-mode guide for tool-calling follow-ups)."""
    from butterfly.core.tool import Tool

    provider = _make_provider()
    captured: list = []
    provider._client = _fake_chat_client(captured)

    async def _noop(**_kw):
        return ""

    tool = Tool(
        name="search", description="Search", func=_noop,
        schema={"type": "object", "properties": {}},
    )

    await provider.complete(
        messages=[
            Message(role="user", content="hi"),
            Message(role="assistant", content=[
                {"type": "reasoning_content", "text": "need to search"},
                {"type": "tool_use", "id": "t1", "name": "search",
                 "input": {"q": "x"}},
            ]),
            Message(role="tool", content=[
                {"type": "tool_result", "tool_use_id": "t1", "content": "hit"},
            ]),
            Message(role="user", content="what next?"),
        ],
        tools=[tool],
        system_prompt="sys",
        model="deepseek-v4-pro",
    )

    call = captured[0]
    assert "tools" in call and call["tools"][0]["function"]["name"] == "search"
    assistant_entries = [m for m in call["messages"] if m["role"] == "assistant"]
    assert len(assistant_entries) == 1
    entry = assistant_entries[0]
    # Tool call preserved AND reasoning_content stamped on the tool-call turn
    # (required by V4-pro's thinking-mode wire contract).
    assert entry["tool_calls"][0]["function"]["name"] == "search"
    assert entry["reasoning_content"] == "need to search"


# ── 7. Registry wiring ────────────────────────────────────────────────────────


def test_registry_deepseek_resolves_to_deepseek_provider(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    from butterfly.llm_engine.registry import resolve_provider

    p = resolve_provider("deepseek")
    assert isinstance(p, DeepSeekProvider)


def test_registry_deepseek_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    from butterfly.llm_engine.registry import resolve_provider

    p = resolve_provider(" DEEPSEEK ")
    assert isinstance(p, DeepSeekProvider)


def test_deepseek_exported_from_llm_engine():
    """``DeepSeekProvider`` is importable from the package top-level so user
    configs can import it without knowing the providers submodule layout."""
    from butterfly import llm_engine

    assert hasattr(llm_engine, "DeepSeekProvider")
    assert llm_engine.DeepSeekProvider is DeepSeekProvider


def test_provider_name_reverse_lookup(monkeypatch):
    """``provider_name(provider)`` returns the registry key for a DS provider."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    from butterfly.llm_engine.registry import provider_name, resolve_provider

    p = resolve_provider("deepseek")
    assert provider_name(p) == "deepseek"


# ── 8. Model catalog wiring — V4 only (founder's opinion) ─────────────────────


def test_catalog_has_deepseek_v4_pro_as_default():
    from butterfly.llm_engine.model_catalog import (
        get_provider_default, get_provider_models, get_model_spec,
    )

    models = get_provider_models("deepseek")
    names = [spec.model for spec in models]
    # Only the V4 family — founder's opinion keeps legacy aliases out.
    assert names == ["deepseek-v4-pro", "deepseek-v4-flash"]

    default = get_provider_default("deepseek")
    assert default is not None
    assert default.model == "deepseek-v4-pro"
    assert default.max_context_tokens == 1_000_000
    assert default.exposes_reasoning_tokens is True

    # Spot-check direct lookup
    spec = get_model_spec("deepseek-v4-flash")
    assert spec is not None
    assert spec.provider == "deepseek"
    assert spec.max_context_tokens == 1_000_000


def test_catalog_excludes_legacy_deepseek_aliases():
    """Legacy ``deepseek-chat`` / ``deepseek-reasoner`` must not be registered
    — per founder's opinion the provider is V4-only and ships no scrubbing
    path for the legacy reasoner."""
    from butterfly.llm_engine.model_catalog import get_model_spec

    assert get_model_spec("deepseek-chat") is None
    assert get_model_spec("deepseek-reasoner") is None


# ── 9. Supports-thinking class flag ───────────────────────────────────────────


def test_deepseek_advertises_thinking_support():
    """The base class inspects ``_supports_thinking`` to decide whether to
    expose thinking toggles in the UI — DeepSeek must advertise True."""
    assert DeepSeekProvider._supports_thinking is True


# ═══════════════════════════════════════════════════════════════════════════
# DeepSeekAnthropicProvider — the Anthropic-compatible /anthropic surface.
# ═══════════════════════════════════════════════════════════════════════════


# ── 10. Class hierarchy & capability flags ────────────────────────────────────


def test_deepseek_anthropic_is_subclass_of_anthropic_provider():
    """The Anthropic variant must subclass the base ``AnthropicProvider`` so
    all message shaping / tool encoding / streaming flows through the
    Anthropic SDK path verbatim."""
    assert issubclass(DeepSeekAnthropicProvider, AnthropicProvider)


def test_deepseek_anthropic_class_flags():
    """Upstream quirks captured as class flags:

    * cache_control is ignored by the DeepSeek gateway → ``supports_cache=False``.
    * anthropic-beta header is ignored → ``thinking_uses_betas=False``.
    * adaptive thinking shape is not recognised → ``supports_adaptive=False``.
    """
    assert DeepSeekAnthropicProvider._supports_cache_control is False
    assert DeepSeekAnthropicProvider._supports_thinking is True
    assert DeepSeekAnthropicProvider._thinking_uses_betas is False
    assert DeepSeekAnthropicProvider._supports_adaptive_thinking is False


# ── 11. Constructor: API key resolution ───────────────────────────────────────


def test_deepseek_anthropic_fails_fast_without_any_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(AuthError) as exc_info:
        DeepSeekAnthropicProvider()
    assert "DEEPSEEK_API_KEY" in str(exc_info.value)


def test_deepseek_anthropic_env_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, max_tokens=8096, base_url=None,
                   default_headers=None):
        captured["api_key"] = api_key

    monkeypatch.setattr(AnthropicProvider, "__init__", _fake_init)
    DeepSeekAnthropicProvider()
    assert captured["api_key"] == "env-key"


def test_deepseek_anthropic_explicit_key_overrides_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, max_tokens=8096, base_url=None,
                   default_headers=None):
        captured["api_key"] = api_key

    monkeypatch.setattr(AnthropicProvider, "__init__", _fake_init)
    DeepSeekAnthropicProvider(api_key="explicit")
    assert captured["api_key"] == "explicit"


# ── 12. Constructor: base URL pinning ─────────────────────────────────────────


def test_deepseek_anthropic_default_base_url(monkeypatch):
    """Default base URL is the /anthropic endpoint. Unlike the OpenAI variant,
    there is no ``DEEPSEEK_BASE_URL`` env override — keeps auth narrow and
    avoids the "which env var actually got used?" debugging rabbit hole."""
    captured: dict[str, object] = {}

    def _fake_init(self, *, api_key=None, max_tokens=8096, base_url=None,
                   default_headers=None):
        captured["base_url"] = base_url

    monkeypatch.setattr(AnthropicProvider, "__init__", _fake_init)
    DeepSeekAnthropicProvider(api_key="k")
    assert captured["base_url"] == _DEEPSEEK_ANTHROPIC_BASE_URL
    assert _DEEPSEEK_ANTHROPIC_BASE_URL == "https://api.deepseek.com/anthropic"


def test_deepseek_anthropic_explicit_base_url(monkeypatch):
    """Constructor ``base_url`` kwarg is honoured — e.g. for gateways."""
    captured: dict[str, object] = {}

    def _fake_init(self, *, api_key=None, max_tokens=8096, base_url=None,
                   default_headers=None):
        captured["base_url"] = base_url

    monkeypatch.setattr(AnthropicProvider, "__init__", _fake_init)
    DeepSeekAnthropicProvider(api_key="k", base_url="https://gateway.example/anth")
    assert captured["base_url"] == "https://gateway.example/anth"


def test_deepseek_anthropic_ignores_deepseek_base_url_env(monkeypatch):
    """The Anthropic variant deliberately does NOT honour ``DEEPSEEK_BASE_URL``
    so the OpenAI-surface override doesn't silently redirect Anthropic traffic
    to a gateway that only speaks OpenAI. Mirrors the Kimi split behaviour."""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://openai-only.example")
    captured: dict[str, object] = {}

    def _fake_init(self, *, api_key=None, max_tokens=8096, base_url=None,
                   default_headers=None):
        captured["base_url"] = base_url

    monkeypatch.setattr(AnthropicProvider, "__init__", _fake_init)
    DeepSeekAnthropicProvider(api_key="k")
    assert captured["base_url"] == _DEEPSEEK_ANTHROPIC_BASE_URL


# ── 13. Registry wiring for the opt-in key ────────────────────────────────────


def test_registry_deepseek_anthropic_resolves_to_anthropic_variant(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    from butterfly.llm_engine.registry import resolve_provider

    p = resolve_provider("deepseek-anthropic")
    assert isinstance(p, DeepSeekAnthropicProvider)
    assert isinstance(p, AnthropicProvider)


def test_registry_default_deepseek_is_still_openai_variant(monkeypatch):
    """Adding the Anthropic opt-in must not change the default ``deepseek``
    key — it should continue to resolve to the OpenAI-shape provider."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    from butterfly.llm_engine.registry import resolve_provider

    p = resolve_provider("deepseek")
    assert isinstance(p, DeepSeekProvider)
    assert not isinstance(p, DeepSeekAnthropicProvider)


def test_deepseek_anthropic_exported_from_llm_engine():
    """Both variants are top-level importable from ``butterfly.llm_engine``."""
    from butterfly import llm_engine

    assert hasattr(llm_engine, "DeepSeekAnthropicProvider")
    assert llm_engine.DeepSeekAnthropicProvider is DeepSeekAnthropicProvider


def test_provider_name_reverse_lookup_for_anthropic(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    from butterfly.llm_engine.registry import provider_name, resolve_provider

    p = resolve_provider("deepseek-anthropic")
    assert provider_name(p) == "deepseek-anthropic"
