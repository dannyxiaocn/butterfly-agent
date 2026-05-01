"""Regression tests for ``butterfly.service.models_service.get_models_catalog``.

The catalog feeds the web UI's provider+model dropdown via ``/api/models``;
a registry entry in ``butterfly/llm_engine/registry.py`` without a matching
``_PROVIDER_META`` row silently disappears from the UI. These tests lock
that invariant so a future provider addition is caught in review rather
than shipping a "works in CLI, invisible in the web UI" regression.
"""
from __future__ import annotations

from butterfly.llm_engine.registry import _REGISTRY
from butterfly.service.models_service import get_models_catalog


def test_every_registry_provider_has_ui_metadata():
    """Every key in the provider registry must appear in the UI catalog.

    Documented exceptions are the opt-in Anthropic-compat aliases
    (``kimi-coding-plan-anthropic``, ``deepseek-anthropic``), which exist
    only for callers who need the Anthropic-shape messages/usage fields
    and are intentionally hidden from the UI dropdown — see the comments
    next to each entry in ``butterfly/llm_engine/registry.py``.
    """
    catalog = get_models_catalog()
    exposed = {p["provider"] for p in catalog["providers"]}
    hidden_by_design = {"kimi-coding-plan-anthropic", "deepseek-anthropic"}
    missing = set(_REGISTRY) - exposed - hidden_by_design
    assert not missing, (
        f"Providers registered in _REGISTRY but absent from the UI catalog: "
        f"{sorted(missing)}. Add a _PROVIDER_META entry in "
        f"butterfly/service/models_service.py so they show up in /api/models."
    )


def test_kimi_public_api_provider_exposed_in_ui_catalog():
    """The public Moonshot provider (added in PR #56) must be UI-visible."""
    catalog = get_models_catalog()
    kimi_entry = next(
        (p for p in catalog["providers"] if p["provider"] == "kimi"),
        None,
    )
    assert kimi_entry is not None, (
        "Registry key 'kimi' is missing from get_models_catalog() output — "
        "users cannot select it from the web UI config editor."
    )
    # Both canonical and alias env vars should be surfaced so the UI can
    # render a complete "set one of these env vars" hint.
    assert "MOONSHOT_API_KEY" in kimi_entry["env"]
    assert "KIMI_API_KEY" in kimi_entry["env"]
    assert kimi_entry["supports_thinking"] is True
    assert kimi_entry["default_model"] == "kimi-k2.6"

    model_names = {m["name"] for m in kimi_entry["models"]}
    assert model_names == {"kimi-k2.6"}, (
        "kimi-k2.6 must be the sole catalog entry — see models.yaml "
        "founder directive. Re-adding k2.5 / k2-thinking / moonshot-v1-* "
        "here requires a YAML change too."
    )


def test_kimi_public_distinct_from_kimi_coding_plan():
    """The two Kimi surfaces must surface as separate dropdown entries."""
    catalog = get_models_catalog()
    providers = {p["provider"]: p for p in catalog["providers"]}
    assert "kimi" in providers
    assert "kimi-coding-plan" in providers
    assert providers["kimi"]["env"] != providers["kimi-coding-plan"]["env"], (
        "Public-API and For-Coding providers use different credentials — "
        "the UI must show different env var hints for each."
    )
    assert providers["kimi"]["default_model"] != providers["kimi-coding-plan"]["default_model"]
