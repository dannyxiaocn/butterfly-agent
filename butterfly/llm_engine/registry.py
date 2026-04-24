from __future__ import annotations
from butterfly.core.provider import Provider

_REGISTRY: dict[str, tuple[str, str]] = {
    "anthropic":                   ("butterfly.llm_engine.providers.anthropic",        "AnthropicProvider"),
    "openai":                      ("butterfly.llm_engine.providers.openai_api",       "OpenAIProvider"),
    "openai-responses":            ("butterfly.llm_engine.providers.openai_responses", "OpenAIResponsesProvider"),
    # Default Kimi entry — OpenAI-compatible surface; returns cached_tokens +
    # reasoning_tokens in usage. Matches kimi-cli's default path.
    "kimi-coding-plan":            ("butterfly.llm_engine.providers.kimi",             "KimiOpenAIProvider"),
    # Opt-in alias for the Anthropic-compatible surface. Existing sessions or
    # callers that need the old behavior (Anthropic-shape messages + usage)
    # should pin this key explicitly.
    "kimi-coding-plan-anthropic":  ("butterfly.llm_engine.providers.kimi",             "KimiAnthropicProvider"),
    "codex-oauth":                 ("butterfly.llm_engine.providers.codex",            "CodexProvider"),
    # DeepSeek: OpenAI-compatible chat completions at api.deepseek.com. V4
    # family supports thinking mode (``reasoning_effort``), tool calls inside
    # thinking mode, and prompt cache hit/miss accounting. Key resolution is
    # strict — ``DEEPSEEK_API_KEY`` is the only env var consulted.
    "deepseek":                    ("butterfly.llm_engine.providers.deepseek",         "DeepSeekProvider"),
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
