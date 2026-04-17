"""Model catalog service — provider → single curated model for the web UI.

Each provider exposes exactly one model (its default). The UI renders the
provider dropdown and reads ``default_model`` as the single available model
— no "(provider default)" label, no long per-provider model list.
"""
from __future__ import annotations

from typing import Any


_MODEL_CATALOG: list[dict[str, Any]] = [
    {
        "provider": "anthropic",
        "label": "Anthropic Claude",
        "env": ["ANTHROPIC_API_KEY"],
        "supports_thinking": True,
        "default_model": "claude-sonnet-4-6",
    },
    {
        "provider": "openai",
        "label": "OpenAI (Chat Completions)",
        "env": ["OPENAI_API_KEY"],
        "supports_thinking": False,
        "default_model": "gpt-4o",
    },
    {
        "provider": "openai-responses",
        "label": "OpenAI (Responses API, reasoning)",
        "env": ["OPENAI_API_KEY"],
        "supports_thinking": True,
        "default_model": "gpt-5",
    },
    {
        "provider": "kimi-coding-plan",
        "label": "Moonshot Kimi (for coding)",
        "env": ["KIMI_FOR_CODING_API_KEY"],
        "supports_thinking": True,
        "default_model": "kimi-for-coding",
    },
    {
        "provider": "codex-oauth",
        "label": "Codex (ChatGPT OAuth)",
        "env": [],  # uses ~/.butterfly/auth.json
        "supports_thinking": True,
        "default_model": "gpt-5.4",
    },
]


def get_models_catalog() -> dict[str, Any]:
    """Return the provider catalog consumed by the web UI config editor."""
    return {"providers": [dict(p) for p in _MODEL_CATALOG]}
