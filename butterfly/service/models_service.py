"""Model catalog service — provider → models list for the web UI.

Provider metadata (label, required env vars, thinking style) lives here and
stays hand-curated so the UI surface matches exactly what the CLI exposes.
Per-model parameters (``max_context_tokens``, ``exposes_reasoning_tokens``,
which entry is the default) come from ``butterfly/llm_engine/models.yaml``
via ``model_catalog``. The ``default_model`` field surfaced on each provider
entry is derived from the yaml's ``default: true`` flag — editing the yaml
is the single knob that changes the UI's default-model hint too.

v2.0.19 (parallel): PR #36 collapsed this service to ``default_model`` only.
This branch restores the ``models: [{name, max_context_tokens, ...}]`` list
shape so future multi-model support is a yaml edit, not a code change; the
list currently contains exactly one entry per provider.
"""
from __future__ import annotations

from typing import Any

from butterfly.llm_engine.model_catalog import get_provider_default, get_provider_models


# Provider metadata. ``default_model`` and the per-model list are filled in
# from models.yaml at ``get_models_catalog`` time so this table stays tight
# on what only the curator knows (UI label, auth hint, thinking style).
_PROVIDER_META: list[dict[str, Any]] = [
    {
        "provider": "anthropic",
        "label": "Anthropic Claude",
        "env": ["ANTHROPIC_API_KEY"],
        "supports_thinking": True,
    },
    {
        "provider": "openai",
        "label": "OpenAI (Chat Completions)",
        "env": ["OPENAI_API_KEY"],
        "supports_thinking": False,
    },
    {
        "provider": "openai-responses",
        "label": "OpenAI (Responses API, reasoning)",
        "env": ["OPENAI_API_KEY"],
        "supports_thinking": True,
    },
    {
        "provider": "kimi-coding-plan",
        "label": "Moonshot Kimi (for coding)",
        "env": ["KIMI_FOR_CODING_API_KEY"],
        "supports_thinking": True,
    },
    {
        "provider": "codex-oauth",
        "label": "Codex (ChatGPT OAuth)",
        "env": [],  # uses ~/.butterfly/auth.json
        "supports_thinking": True,
    },
]


def _models_for(provider: str) -> list[dict[str, Any]]:
    """Return the yaml-sourced model list for a provider, serialized for JSON.

    Empty list when the provider has no entries in models.yaml — the UI
    renders an empty dropdown rather than crashing.
    """
    return [
        {
            "name": spec.model,
            "max_context_tokens": spec.max_context_tokens,
            "exposes_reasoning_tokens": spec.exposes_reasoning_tokens,
            "default": spec.default,
        }
        for spec in get_provider_models(provider)
    ]


def get_models_catalog() -> dict[str, Any]:
    """Return the provider catalog consumed by the web UI config editor.

    Shape:
        {"providers": [
            {"provider": "...", "label": "...", "env": [...],
             "supports_thinking": bool, "default_model": "...",
             "models": [
                {"name": "...", "max_context_tokens": int,
                 "exposes_reasoning_tokens": bool, "default": bool},
                ...
             ]},
            ...
        ]}
    """
    providers: list[dict[str, Any]] = []
    for meta in _PROVIDER_META:
        entry = dict(meta)
        default_spec = get_provider_default(meta["provider"])
        entry["default_model"] = default_spec.model if default_spec else ""
        entry["models"] = _models_for(meta["provider"])
        providers.append(entry)
    return {"providers": providers}
