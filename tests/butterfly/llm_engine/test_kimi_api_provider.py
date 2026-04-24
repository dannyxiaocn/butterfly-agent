"""Unit tests for ``KimiProvider`` (Moonshot standalone API).

``KimiProvider`` subclasses ``OpenAIProvider`` and talks to Moonshot's
public OpenAI-compatible surface at ``api.moonshot.ai/v1/chat/completions``.
The contract it adds on top of the base class:

- Resolve ``api_key`` from explicit arg → ``MOONSHOT_API_KEY`` →
  ``KIMI_API_KEY``. Fails fast with :class:`AuthError` when none is set.
- Resolve ``base_url`` from explicit arg → ``MOONSHOT_BASE_URL`` →
  ``https://api.moonshot.ai/v1`` (so China-region users can swap endpoints
  with a single env var).
- Inject ``extra_body={"thinking": {"type": "enabled", "keep": "all"}}``
  when ``thinking=True``; omit the key otherwise. The ``keep`` flag is
  configurable through the constructor but defaults to ``"all"`` so the
  reasoning_content we already echo back gets honored server-side.
- Forward an optional ``prompt_cache_key`` via ``extra_body`` on every
  request when configured (independent of ``thinking``).
- Extract ``cached_tokens`` from Moonshot's top-level usage attribute when
  the standard ``prompt_tokens_details.cached_tokens`` is absent.
- Registers under the ``"kimi"`` key, distinct from ``"kimi-coding-plan"``.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from butterfly.core.types import Message, TokenUsage
from butterfly.llm_engine.errors import AuthError
from butterfly.llm_engine.providers.kimi_api import (
    KimiProvider,
    _MOONSHOT_DEFAULT_BASE_URL,
)
from butterfly.llm_engine.providers.openai_api import OpenAIProvider


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_provider(
    *,
    prompt_cache_key: str | None = None,
    thinking_keep: str | None = "all",
) -> KimiProvider:
    """Construct a KimiProvider without calling the real OpenAI SDK."""
    p = KimiProvider.__new__(KimiProvider)
    p.max_tokens = 8096
    p._client = None  # patched per-test
    p._prompt_cache_key = prompt_cache_key
    p._thinking_keep = thinking_keep
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


# ── 1. Constructor: base URL resolution ───────────────────────────────────────


def test_default_base_url_is_international_moonshot(monkeypatch):
    """Absent any env override, the international endpoint is used."""
    monkeypatch.setenv("MOONSHOT_API_KEY", "k")
    monkeypatch.delenv("MOONSHOT_BASE_URL", raising=False)
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, base_url=None, max_tokens=8096, max_retries=3, default_headers=None):
        captured["base_url"] = base_url
        captured["api_key"] = api_key

    monkeypatch.setattr(OpenAIProvider, "__init__", _fake_init)
    KimiProvider()
    assert captured["base_url"] == _MOONSHOT_DEFAULT_BASE_URL
    assert captured["base_url"] == "https://api.moonshot.ai/v1"


def test_base_url_env_var_override_wins(monkeypatch):
    """``MOONSHOT_BASE_URL`` swaps the endpoint (e.g. CN region)."""
    monkeypatch.setenv("MOONSHOT_API_KEY", "k")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1")
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, base_url=None, max_tokens=8096, max_retries=3, default_headers=None):
        captured["base_url"] = base_url

    monkeypatch.setattr(OpenAIProvider, "__init__", _fake_init)
    KimiProvider()
    assert captured["base_url"] == "https://api.moonshot.cn/v1"


def test_explicit_base_url_beats_env_var(monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "k")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1")
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, base_url=None, max_tokens=8096, max_retries=3, default_headers=None):
        captured["base_url"] = base_url

    monkeypatch.setattr(OpenAIProvider, "__init__", _fake_init)
    KimiProvider(base_url="https://custom.kimi.example/v1")
    assert captured["base_url"] == "https://custom.kimi.example/v1"


# ── 2. Constructor: API key resolution ────────────────────────────────────────


def test_fails_fast_without_any_key(monkeypatch):
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)

    with pytest.raises(AuthError) as exc_info:
        KimiProvider()
    # Error message must mention both accepted env var names so the user
    # knows what to set without digging through the source.
    assert "MOONSHOT_API_KEY" in str(exc_info.value)
    assert "KIMI_API_KEY" in str(exc_info.value)


def test_primary_env_key_moonshot(monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "primary")
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, base_url=None, max_tokens=8096, max_retries=3, default_headers=None):
        captured["api_key"] = api_key

    monkeypatch.setattr(OpenAIProvider, "__init__", _fake_init)
    KimiProvider()
    assert captured["api_key"] == "primary"


def test_kimi_api_key_alias_accepted(monkeypatch):
    """``KIMI_API_KEY`` alias is honored when the canonical var is unset."""
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.setenv("KIMI_API_KEY", "alias-key")
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, base_url=None, max_tokens=8096, max_retries=3, default_headers=None):
        captured["api_key"] = api_key

    monkeypatch.setattr(OpenAIProvider, "__init__", _fake_init)
    KimiProvider()
    assert captured["api_key"] == "alias-key"


def test_moonshot_wins_over_kimi_when_both_set(monkeypatch):
    """Canonical ``MOONSHOT_API_KEY`` beats the ``KIMI_API_KEY`` alias."""
    monkeypatch.setenv("MOONSHOT_API_KEY", "canonical")
    monkeypatch.setenv("KIMI_API_KEY", "alias")
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, base_url=None, max_tokens=8096, max_retries=3, default_headers=None):
        captured["api_key"] = api_key

    monkeypatch.setattr(OpenAIProvider, "__init__", _fake_init)
    KimiProvider()
    assert captured["api_key"] == "canonical"


def test_explicit_key_beats_env(monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "env-key")
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, base_url=None, max_tokens=8096, max_retries=3, default_headers=None):
        captured["api_key"] = api_key

    monkeypatch.setattr(OpenAIProvider, "__init__", _fake_init)
    KimiProvider(api_key="explicit")
    assert captured["api_key"] == "explicit"


def test_no_kimi_for_coding_env_var_fallback(monkeypatch):
    """The public API provider must NOT fall back to the Kimi For Coding key —
    they authenticate against different gateways.
    """
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.setenv("KIMI_FOR_CODING_API_KEY", "coding-key")

    with pytest.raises(AuthError):
        KimiProvider()


# ── 3. Thinking behavior ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_thinking_enabled_injects_extra_body_with_keep_all():
    """Default ``thinking_keep='all'`` shows up in the thinking payload."""
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_chat_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2.6",
        thinking=True,
    )

    call = captured[0]
    assert call.get("extra_body") == {
        "thinking": {"type": "enabled", "keep": "all"},
    }


@pytest.mark.asyncio
async def test_thinking_disabled_omits_extra_body_when_no_cache_key():
    provider = _make_provider()
    captured: list = []
    provider._client = _fake_chat_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="moonshot-v1-auto",
    )

    call = captured[0]
    assert "extra_body" not in call


@pytest.mark.asyncio
async def test_thinking_keep_can_be_disabled():
    """``thinking_keep=None`` drops the ``keep`` field but still enables."""
    provider = _make_provider(thinking_keep=None)
    captured: list = []
    provider._client = _fake_chat_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2.6",
        thinking=True,
    )

    assert captured[0]["extra_body"] == {"thinking": {"type": "enabled"}}


@pytest.mark.asyncio
async def test_empty_string_thinking_keep_normalizes_to_none(monkeypatch):
    """Empty string is treated as None so ``keep`` is not serialized."""
    monkeypatch.setenv("MOONSHOT_API_KEY", "k")
    captured: dict[str, Any] = {}

    def _fake_init(self, *, api_key=None, base_url=None, max_tokens=8096, max_retries=3, default_headers=None):
        captured["_"] = True

    monkeypatch.setattr(OpenAIProvider, "__init__", _fake_init)
    p = KimiProvider(thinking_keep="")
    assert p._thinking_keep is None


# ── 4. prompt_cache_key passthrough ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_cache_key_attached_to_every_request():
    """When set, ``prompt_cache_key`` is sent on each request via extra_body."""
    provider = _make_provider(prompt_cache_key="session-42")
    captured: list = []
    provider._client = _fake_chat_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="moonshot-v1-128k",
    )

    assert captured[0]["extra_body"] == {"prompt_cache_key": "session-42"}


@pytest.mark.asyncio
async def test_prompt_cache_key_merges_with_thinking():
    provider = _make_provider(prompt_cache_key="session-42")
    captured: list = []
    provider._client = _fake_chat_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2.6",
        thinking=True,
    )

    extra_body = captured[0]["extra_body"]
    assert extra_body["prompt_cache_key"] == "session-42"
    assert extra_body["thinking"] == {"type": "enabled", "keep": "all"}


@pytest.mark.asyncio
async def test_no_prompt_cache_key_means_no_field():
    provider = _make_provider(prompt_cache_key=None)
    captured: list = []
    provider._client = _fake_chat_client(captured)

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="moonshot-v1-128k",
    )

    assert "extra_body" not in captured[0]


# ── 5. Usage extraction ───────────────────────────────────────────────────────


def test_usage_prefers_prompt_tokens_details_when_present():
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=40,
        prompt_tokens_details=SimpleNamespace(cached_tokens=30),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=5),
    )
    out = KimiProvider._extract_usage(usage)
    assert out == TokenUsage(
        input_tokens=70,
        output_tokens=40,
        cache_read_tokens=30,
        cache_write_tokens=0,
        reasoning_tokens=5,
    )


def test_usage_falls_back_to_top_level_cached_tokens():
    usage = SimpleNamespace(
        prompt_tokens=80,
        completion_tokens=20,
        cached_tokens=25,
        prompt_tokens_details=None,
        completion_tokens_details=None,
    )
    out = KimiProvider._extract_usage(usage)
    assert out.cache_read_tokens == 25
    assert out.input_tokens == 55
    assert out.output_tokens == 20


def test_usage_no_cache_at_all():
    usage = SimpleNamespace(
        prompt_tokens=50,
        completion_tokens=10,
        prompt_tokens_details=None,
        completion_tokens_details=None,
    )
    out = KimiProvider._extract_usage(usage)
    assert out == TokenUsage(input_tokens=50, output_tokens=10)


def test_usage_prompt_details_wins_over_top_level():
    """If both shapes are populated, the standard field takes precedence."""
    usage = SimpleNamespace(
        prompt_tokens=200,
        completion_tokens=40,
        cached_tokens=99,
        prompt_tokens_details=SimpleNamespace(cached_tokens=60),
        completion_tokens_details=None,
    )
    out = KimiProvider._extract_usage(usage)
    assert out.cache_read_tokens == 60
    assert out.input_tokens == 140


# ── 6. End-to-end: non-streaming path ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_returns_text_and_usage():
    provider = _make_provider()

    async def _create(**_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="hello", tool_calls=None))
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
        model="kimi-k2.6",
    )
    assert text == "hello"
    assert tool_calls == []
    assert usage.cache_read_tokens == 20
    assert usage.input_tokens == 100
    assert usage.output_tokens == 12


# ── 7. Registry wiring ────────────────────────────────────────────────────────


def test_registry_kimi_resolves_to_kimi_provider(monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "fake")
    from butterfly.llm_engine.registry import resolve_provider

    p = resolve_provider("kimi")
    assert isinstance(p, KimiProvider)


def test_registry_kimi_distinct_from_kimi_coding_plan(monkeypatch):
    """``kimi`` and ``kimi-coding-plan`` must resolve to different classes."""
    monkeypatch.setenv("MOONSHOT_API_KEY", "pub")
    monkeypatch.setenv("KIMI_FOR_CODING_API_KEY", "coding")
    from butterfly.llm_engine.providers.kimi import KimiOpenAIProvider
    from butterfly.llm_engine.registry import resolve_provider

    pub = resolve_provider("kimi")
    coding = resolve_provider("kimi-coding-plan")
    assert isinstance(pub, KimiProvider)
    assert isinstance(coding, KimiOpenAIProvider)
    assert type(pub) is not type(coding)


def test_registry_reverse_lookup():
    """Reverse registry lookup returns ``"kimi"`` for the public provider."""
    from butterfly.llm_engine.registry import provider_name

    # Use a bare instance so we don't need env vars set.
    instance = KimiProvider.__new__(KimiProvider)
    assert provider_name(instance) == "kimi"


# ── 8. Reasoning content round-trip (inherited machinery) ────────────────────


def _fake_stream_client(chunks: list) -> SimpleNamespace:
    async def _create(**_kwargs: Any) -> Any:
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


@pytest.mark.asyncio
async def test_stream_captures_reasoning_content_for_round_trip():
    """Inherited behavior: ``delta.reasoning_content`` accumulates so the
    assistant-message echo can replay it on the next turn — Kimi's
    ``keep: "all"`` mode requires this.
    """
    provider = _make_provider()
    chunks = [
        _chunk(reasoning="let me "),
        _chunk(reasoning="think."),
        _chunk(content="done."),
        SimpleNamespace(choices=[], usage=SimpleNamespace(
            prompt_tokens=5, completion_tokens=2,
            prompt_tokens_details=None, completion_tokens_details=None,
        )),
    ]
    provider._client = _fake_stream_client(chunks)

    text, tool_calls, _usage = await provider.complete(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system_prompt="sys",
        model="kimi-k2.6",
        thinking=True,
        on_text_chunk=lambda _: None,
    )
    assert text == "done."
    assert tool_calls == []
    assert provider._pending_reasoning_content == "let me think."


# ── 9. Model catalog registration ────────────────────────────────────────────


def test_model_catalog_has_kimi_k2_6_only():
    """Per founder directive, the catalog exposes only ``kimi-k2.6``.

    Older k2.5 / k2-thinking / moonshot-v1-* families are deliberately
    omitted to keep the surface small. Test locks the invariant so a
    future YAML edit re-introducing them is caught in review.
    """
    from butterfly.llm_engine.model_catalog import (
        get_provider_default,
        get_provider_models,
    )

    specs = get_provider_models("kimi")
    names = {s.model for s in specs}
    assert names == {"kimi-k2.6"}

    default = get_provider_default("kimi")
    assert default is not None
    assert default.model == "kimi-k2.6"
    assert default.exposes_reasoning_tokens is True


def test_public_api_models_distinct_from_coding_plan():
    from butterfly.llm_engine.model_catalog import get_provider_models

    public = {s.model for s in get_provider_models("kimi")}
    coding = {s.model for s in get_provider_models("kimi-coding-plan")}
    # kimi-for-coding lives only under the coding plan; kimi-k2.6 only under public.
    assert "kimi-for-coding" in coding
    assert "kimi-for-coding" not in public
    assert "kimi-k2.6" in public
    assert "kimi-k2.6" not in coding
