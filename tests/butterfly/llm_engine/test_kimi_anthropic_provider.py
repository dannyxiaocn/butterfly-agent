"""Name/identity tests for ``KimiAnthropicProvider``.

The existing ``test_kimi_provider.py`` covers the behavior of the
Anthropic-compat Kimi provider via its historical name
``KimiForCodingProvider`` (kept as an alias). This file adds the small set
of assertions specific to the rename so a future cleanup of the alias
doesn't silently lose coverage.
"""
from __future__ import annotations

import pytest

from butterfly.llm_engine.errors import AuthError
from butterfly.llm_engine.providers.anthropic import AnthropicProvider
from butterfly.llm_engine.providers.kimi import (
    KimiAnthropicProvider,
    KimiForCodingProvider,
    _KIMI_ANTHROPIC_BASE_URL,
)


def test_kimi_anthropic_is_subclass_of_anthropic_provider():
    assert issubclass(KimiAnthropicProvider, AnthropicProvider)


def test_kimi_for_coding_alias_matches_anthropic_variant():
    assert KimiForCodingProvider is KimiAnthropicProvider


def test_kimi_anthropic_class_flags():
    assert KimiAnthropicProvider._supports_cache_control is False
    assert KimiAnthropicProvider._supports_thinking is True
    assert KimiAnthropicProvider._thinking_uses_betas is False


def test_kimi_anthropic_fails_fast_without_any_key(monkeypatch):
    monkeypatch.delenv("KIMI_FOR_CODING_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)

    with pytest.raises(AuthError):
        KimiAnthropicProvider()


def test_kimi_anthropic_default_base_url(monkeypatch):
    monkeypatch.delenv("KIMI_BASE_URL", raising=False)
    captured: dict[str, object] = {}

    def _fake_init(self, *, api_key=None, max_tokens=8096, base_url=None, default_headers=None):
        captured["base_url"] = base_url

    monkeypatch.setattr(AnthropicProvider, "__init__", _fake_init)
    KimiAnthropicProvider(api_key="k")
    assert captured["base_url"] == _KIMI_ANTHROPIC_BASE_URL


def test_registry_opt_in_key_resolves_to_anthropic(monkeypatch):
    monkeypatch.setenv("KIMI_FOR_CODING_API_KEY", "fake")
    from butterfly.llm_engine.registry import resolve_provider

    p = resolve_provider("kimi-coding-plan-anthropic")
    assert isinstance(p, KimiAnthropicProvider)
