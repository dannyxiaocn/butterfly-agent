from __future__ import annotations
from butterfly.core.provider import Provider

_REGISTRY: dict[str, tuple[str, str]] = {
    "anthropic":         ("butterfly.llm_engine.providers.anthropic",        "AnthropicProvider"),
    "openai":            ("butterfly.llm_engine.providers.openai_api",       "OpenAIProvider"),
    "openai-responses":  ("butterfly.llm_engine.providers.openai_responses", "OpenAIResponsesProvider"),
    "kimi-coding-plan":  ("butterfly.llm_engine.providers.kimi",             "KimiForCodingProvider"),
    "codex-oauth":       ("butterfly.llm_engine.providers.codex",            "CodexProvider"),
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
