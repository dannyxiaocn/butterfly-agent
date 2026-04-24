"""Tests for ``butterfly.service.models_service``.

The models catalog feeds the web UI's ``/api/models`` endpoint and the
config editor's provider dropdown. The invariant these tests lock in is:

    Any provider that ships model entries in ``models.yaml`` AND is
    user-selectable via the registry MUST appear in ``_PROVIDER_META`` —
    otherwise the web UI silently drops it from the dropdown even though
    the CLI happily resolves it.

Historically, providers have been added to ``registry.py`` + ``models.yaml``
but forgotten in ``_PROVIDER_META`` (e.g. PR #55's initial DeepSeek landing).
This test suite enforces the parity so the next provider addition can't
repeat the oversight.
"""
from __future__ import annotations

from butterfly.llm_engine.model_catalog import get_provider_models
from butterfly.llm_engine.registry import _REGISTRY
from butterfly.service.models_service import _PROVIDER_META, get_models_catalog


# Registry keys that are intentionally not exposed in the web UI — keep them
# out of _PROVIDER_META on purpose. Both listed keys are opt-in aliases for
# the Anthropic-compatible surface of their provider and share their UI
# surface with the default OpenAI-shape entry, so the UI does not list them
# separately (selecting "DeepSeek V4" / "Moonshot Kimi" in the dropdown
# implicitly picks the OpenAI surface; callers who specifically want the
# Anthropic shape pin these keys in YAML directly).
_UI_OMITTED_REGISTRY_KEYS: frozenset[str] = frozenset({
    "kimi-coding-plan-anthropic",
    "deepseek-anthropic",
})


def test_every_yaml_provider_has_ui_metadata() -> None:
    """Every provider with models in yaml is enumerable in the web UI."""
    ui_providers = {entry["provider"] for entry in _PROVIDER_META}
    for registry_key in _REGISTRY:
        if registry_key in _UI_OMITTED_REGISTRY_KEYS:
            continue
        models = get_provider_models(registry_key)
        if not models:
            # Providers without yaml entries (if any) skip UI enumeration.
            continue
        assert registry_key in ui_providers, (
            f"Provider '{registry_key}' has entries in models.yaml but is "
            f"missing from _PROVIDER_META in models_service.py — the web UI "
            f"will silently drop it from the /api/models dropdown. Either add "
            f"a metadata entry or document the omission in "
            f"_UI_OMITTED_REGISTRY_KEYS."
        )


def test_deepseek_is_enumerated_in_catalog() -> None:
    """DeepSeek appears in the UI catalog with its models and default set."""
    catalog = get_models_catalog()
    providers_by_key = {p["provider"]: p for p in catalog["providers"]}

    assert "deepseek" in providers_by_key, (
        "DeepSeek is registered in models.yaml and the registry but is "
        "missing from the web UI provider catalog."
    )
    entry = providers_by_key["deepseek"]
    assert entry["label"]  # non-empty human label
    assert entry["env"] == ["DEEPSEEK_API_KEY"]
    assert entry["supports_thinking"] is True
    assert entry["default_model"] == "deepseek-v4-pro"

    # Scope — from founder's opinion: only the V4 family is surfaced. The
    # legacy ``deepseek-chat`` / ``deepseek-reasoner`` aliases are explicitly
    # out of scope (obsolete, add complexity, deprecated 2026-07 upstream).
    model_names = [m["name"] for m in entry["models"]]
    assert model_names == ["deepseek-v4-pro", "deepseek-v4-flash"], (
        f"DeepSeek UI catalog must expose only the V4 family; got {model_names}."
    )


def test_provider_meta_keys_all_resolvable() -> None:
    """Every ``_PROVIDER_META`` entry points at a real registry key — the
    reverse of ``test_every_yaml_provider_has_ui_metadata``, and guards
    against a stale UI entry referencing a removed provider.
    """
    for entry in _PROVIDER_META:
        assert entry["provider"] in _REGISTRY, (
            f"_PROVIDER_META references '{entry['provider']}' which is not "
            f"in the registry. Either remove the metadata or register the "
            f"provider."
        )
