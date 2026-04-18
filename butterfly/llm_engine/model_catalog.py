"""Model catalog loader — reads models.yaml and exposes per-model parameters.

The YAML file is the source of truth for per-model parameters that the rest
of the system needs at runtime (context window size, whether reasoning
tokens are exposed in usage, which entry is the provider's default).

Callers should use the public helpers here instead of hardcoding values —
the web UI used to carry a duplicate ``_MODEL_MAX_TOKENS`` table that
silently drifted from reality, which is the bug this module closes.

Loading is lazy (first call) and cached. To reload after editing the YAML
at runtime, call ``reload_catalog()``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


_DEFAULT_MAX_CONTEXT = 200_000
_CATALOG_PATH = Path(__file__).with_name("models.yaml")


@dataclass(frozen=True)
class ModelSpec:
    model: str
    provider: str
    max_context_tokens: int
    exposes_reasoning_tokens: bool
    default: bool


# ``by_model[model_name] = ModelSpec`` for direct lookup.
# ``by_provider[provider_key] = [ModelSpec, ...]`` preserving YAML order.
_by_model: dict[str, ModelSpec] | None = None
_by_provider: dict[str, list[ModelSpec]] | None = None


def _load() -> tuple[dict[str, ModelSpec], dict[str, list[ModelSpec]]]:
    by_model: dict[str, ModelSpec] = {}
    by_provider: dict[str, list[ModelSpec]] = {}
    if not _CATALOG_PATH.exists():
        return by_model, by_provider
    raw: Any = yaml.safe_load(_CATALOG_PATH.read_text(encoding="utf-8")) or {}
    providers = raw.get("providers") or {}
    for provider_key, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        models = entry.get("models") or []
        if not isinstance(models, list):
            continue
        specs: list[ModelSpec] = []
        for model_entry in models:
            if not isinstance(model_entry, dict):
                continue
            name = model_entry.get("name")
            if not name:
                continue
            spec = ModelSpec(
                model=name,
                provider=provider_key,
                max_context_tokens=int(
                    model_entry.get("max_context_tokens") or _DEFAULT_MAX_CONTEXT
                ),
                exposes_reasoning_tokens=bool(
                    model_entry.get("exposes_reasoning_tokens", False)
                ),
                default=bool(model_entry.get("default", False)),
            )
            specs.append(spec)
            # When the same model name appears under multiple providers the
            # last definition wins — matches the intuition that the model_id
            # string itself is the lookup key. Duplicates aren't expected
            # today (each model lives under one provider).
            by_model[name] = spec
        by_provider[provider_key] = specs
    return by_model, by_provider


def _ensure_loaded() -> tuple[dict[str, ModelSpec], dict[str, list[ModelSpec]]]:
    global _by_model, _by_provider
    if _by_model is None or _by_provider is None:
        _by_model, _by_provider = _load()
    return _by_model, _by_provider


def reload_catalog() -> None:
    """Drop the cached catalog so the next access re-reads models.yaml."""
    global _by_model, _by_provider
    _by_model = None
    _by_provider = None


def get_model_spec(model: str | None) -> ModelSpec | None:
    if not model:
        return None
    by_model, _ = _ensure_loaded()
    return by_model.get(model)


def get_max_context_tokens(model: str | None, default: int = _DEFAULT_MAX_CONTEXT) -> int:
    spec = get_model_spec(model)
    return spec.max_context_tokens if spec else default


def get_provider_models(provider: str) -> list[ModelSpec]:
    """All models registered under a provider key, in YAML order.

    Returns an empty list when the provider isn't in the catalog — callers
    shouldn't crash on unknown providers (user may be editing a config that
    targets a provider not yet catalogued).
    """
    _, by_provider = _ensure_loaded()
    return list(by_provider.get(provider, []))


def get_provider_default(provider: str) -> ModelSpec | None:
    """The model entry flagged ``default: true`` under a provider.

    Returns the first ``default: true`` match, or the first model in the
    list if none is explicitly flagged (cheap fallback that keeps the UI
    functional when someone adds a model entry without updating the flag).
    Returns None for empty / unknown providers.
    """
    models = get_provider_models(provider)
    for spec in models:
        if spec.default:
            return spec
    return models[0] if models else None


def all_specs() -> dict[str, ModelSpec]:
    by_model, _ = _ensure_loaded()
    return dict(by_model)


__all__ = [
    "ModelSpec",
    "all_specs",
    "get_max_context_tokens",
    "get_model_spec",
    "get_provider_default",
    "get_provider_models",
    "reload_catalog",
]
