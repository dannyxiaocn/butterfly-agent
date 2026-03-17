from __future__ import annotations
from nutshell.providers import Provider

_REGISTRY: dict[str, tuple[str, str]] = {
    "anthropic":        ("nutshell.providers.llm.anthropic", "AnthropicProvider"),
    "kimi-coding-plan": ("nutshell.providers.llm.kimi",      "KimiForCodingProvider"),
}


def resolve_provider(name: str) -> Provider:
    """Create a provider instance by name. Imports lazily."""
    key = name.lower().strip()
    if key not in _REGISTRY:
        raise ValueError(f"Unknown provider '{name}'. Available: {sorted(_REGISTRY)}")
    module_path, class_name = _REGISTRY[key]
    import importlib
    return getattr(importlib.import_module(module_path), class_name)()


def provider_name(provider: Provider | None) -> str | None:
    """Reverse-lookup: registry key for a provider instance, or None."""
    if provider is None:
        return None
    cls_name = type(provider).__name__
    return next((k for k, (_, c) in _REGISTRY.items() if c == cls_name), None)
