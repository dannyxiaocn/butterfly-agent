"""Tests for the model catalog loader.

Covers the v2.0.31 catalog extension that added optional Anthropic-specific
thinking/cache knobs to ``ModelSpec``. The invariants under test:

1. A YAML entry with *all* new fields populates every ``ModelSpec`` attribute.
2. A YAML entry with *none* of the new fields (the pre-v2.0.31 shape) still
   loads — every new attribute defaults to ``None``, no crash or type error.
3. A YAML entry with a *partial* set of new fields keeps the rest as ``None``
   without leaking values across entries.

These tests drive ``_load`` by pointing ``_CATALOG_PATH`` at a tmp file and
calling ``reload_catalog()`` to drop the module-level cache.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from butterfly.llm_engine import model_catalog


@pytest.fixture
def stub_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Yield a helper that writes YAML to a tmp file and repoints the loader."""
    yaml_path = tmp_path / "models.yaml"

    def _install(yaml_text: str) -> None:
        yaml_path.write_text(yaml_text, encoding="utf-8")
        monkeypatch.setattr(model_catalog, "_CATALOG_PATH", yaml_path)
        model_catalog.reload_catalog()

    yield _install
    # Restore the real catalog for downstream tests that import the module.
    model_catalog.reload_catalog()


def test_parse_with_all_new_fields(stub_catalog):
    stub_catalog(
        """
providers:
  anthropic:
    models:
      - name: claude-sonnet-4-6
        max_context_tokens: 200000
        exposes_reasoning_tokens: false
        default: true
        thinking_mode: adaptive
        thinking_effort: high
        thinking_display: summarized
        thinking_budget_tokens: 8000
        interleaved_thinking_beta: false
        cache_strategy: two_plus_two
        cache_ttl: 5m
"""
    )

    spec = model_catalog.get_model_spec("claude-sonnet-4-6")
    assert spec is not None
    # Legacy fields still parse cleanly.
    assert spec.provider == "anthropic"
    assert spec.max_context_tokens == 200_000
    assert spec.exposes_reasoning_tokens is False
    assert spec.default is True
    # New fields all materialize with their YAML values.
    assert spec.thinking_mode == "adaptive"
    assert spec.thinking_effort == "high"
    assert spec.thinking_display == "summarized"
    assert spec.thinking_budget_tokens == 8000
    assert spec.interleaved_thinking_beta is False
    assert spec.cache_strategy == "two_plus_two"
    assert spec.cache_ttl == "5m"


def test_parse_without_new_fields_defaults_to_none(stub_catalog):
    # Pre-v2.0.31 YAML shape — the exact bytes that shipped in v2.0.30.
    stub_catalog(
        """
providers:
  openai:
    models:
      - name: gpt-4o
        max_context_tokens: 200000
        exposes_reasoning_tokens: false
        default: true
"""
    )

    spec = model_catalog.get_model_spec("gpt-4o")
    assert spec is not None
    assert spec.provider == "openai"
    assert spec.max_context_tokens == 200_000
    assert spec.default is True
    # Every optional new field falls back to None — no exceptions, no defaults
    # injected by the loader.
    assert spec.thinking_mode is None
    assert spec.thinking_effort is None
    assert spec.thinking_display is None
    assert spec.thinking_budget_tokens is None
    assert spec.interleaved_thinking_beta is None
    assert spec.cache_strategy is None
    assert spec.cache_ttl is None


def test_parse_with_partial_new_fields(stub_catalog):
    stub_catalog(
        """
providers:
  anthropic:
    models:
      - name: claude-sonnet-4-6
        max_context_tokens: 200000
        exposes_reasoning_tokens: false
        default: true
        thinking_mode: enabled
        thinking_budget_tokens: 4096
"""
    )

    spec = model_catalog.get_model_spec("claude-sonnet-4-6")
    assert spec is not None
    # Specified keys come through.
    assert spec.thinking_mode == "enabled"
    assert spec.thinking_budget_tokens == 4096
    # Absent keys remain None — no leakage from a sibling model or default.
    assert spec.thinking_effort is None
    assert spec.thinking_display is None
    assert spec.interleaved_thinking_beta is None
    assert spec.cache_strategy is None
    assert spec.cache_ttl is None


def test_new_fields_do_not_leak_across_entries(stub_catalog):
    """Two Anthropic entries with different partial sets keep their values isolated."""
    stub_catalog(
        """
providers:
  anthropic:
    models:
      - name: claude-sonnet-4-6
        max_context_tokens: 200000
        exposes_reasoning_tokens: false
        default: true
        thinking_mode: adaptive
        cache_strategy: two_plus_two
      - name: claude-opus-4-7
        max_context_tokens: 200000
        exposes_reasoning_tokens: true
        default: false
        thinking_mode: adaptive
        thinking_effort: max
        thinking_display: omitted
"""
    )

    sonnet = model_catalog.get_model_spec("claude-sonnet-4-6")
    opus = model_catalog.get_model_spec("claude-opus-4-7")
    assert sonnet is not None and opus is not None

    # Sonnet has cache_strategy but no effort/display.
    assert sonnet.cache_strategy == "two_plus_two"
    assert sonnet.thinking_effort is None
    assert sonnet.thinking_display is None

    # Opus has effort/display but no cache_strategy — no cross-contamination.
    assert opus.thinking_effort == "max"
    assert opus.thinking_display == "omitted"
    assert opus.cache_strategy is None


def test_shipping_yaml_exposes_anthropic_fields():
    """Sanity-check the checked-in models.yaml — Anthropic entry has the new fields."""
    # Drop any cache a previous test may have left.
    model_catalog.reload_catalog()
    spec = model_catalog.get_model_spec("claude-sonnet-4-6")
    assert spec is not None
    assert spec.thinking_mode == "adaptive"
    assert spec.thinking_effort == "high"
    assert spec.thinking_display == "summarized"
    assert spec.thinking_budget_tokens == 8000
    assert spec.interleaved_thinking_beta is False
    assert spec.cache_strategy == "two_plus_two"
    assert spec.cache_ttl == "5m"

    # Non-Anthropic providers are unaffected: every new attribute stays None.
    gpt = model_catalog.get_model_spec("gpt-4o")
    assert gpt is not None
    assert gpt.thinking_mode is None
    assert gpt.thinking_effort is None
    assert gpt.thinking_display is None
    assert gpt.thinking_budget_tokens is None
    assert gpt.interleaved_thinking_beta is None
    assert gpt.cache_strategy is None
    assert gpt.cache_ttl is None
