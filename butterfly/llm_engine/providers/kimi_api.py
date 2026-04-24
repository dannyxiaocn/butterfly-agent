"""Kimi (Moonshot) standalone API provider.

This is the **public** Kimi API hosted at ``api.moonshot.ai`` (international)
or ``api.moonshot.cn`` (China) — distinct from the Kimi For Coding gateway
at ``api.kimi.com/coding/`` which has its own provider in ``kimi.py``. The
two surfaces use different auth credentials, different base URLs, and
different access-control rules (the For Coding gateway rejects requests
without a coding-agent ``User-Agent``; the public API does not).

The public API is OpenAI-compatible at ``/v1/chat/completions``, so this
provider subclasses :class:`OpenAIProvider` and only overrides what Kimi
adds on top:

- **Auth**: resolved from ``MOONSHOT_API_KEY`` (official) or ``KIMI_API_KEY``
  (alias for convenience), or an explicit ``api_key`` argument. Both
  environment variables are accepted because Moonshot's docs consistently
  use ``MOONSHOT_API_KEY`` but the Chinese community often names their
  variable ``KIMI_API_KEY``; accepting both avoids friction.
- **Base URL**: defaults to ``https://api.moonshot.ai/v1`` (international
  endpoint). Overridable via the ``base_url`` argument or
  ``MOONSHOT_BASE_URL`` env var so China-region users can point at
  ``https://api.moonshot.cn/v1`` without code changes.
- **Thinking**: enabled via ``extra_body={"thinking": {"type": "enabled",
  "keep": "all"}}``. The ``keep: "all"`` flag tells Kimi to honor the
  reasoning_content echoed back on subsequent turns (the base class's
  ``consume_extra_blocks`` machinery already attaches it); without
  ``keep`` set the server silently drops historical reasoning, defeating
  the round-trip we already pay for.
- **Prompt caching**: optional ``prompt_cache_key`` constructor argument.
  When set, every request includes ``prompt_cache_key=<value>`` which
  lets Moonshot's server-side cache match across sessions. Cached tokens
  are reported in ``usage.cached_tokens`` and the custom
  ``_extract_usage`` below handles both the top-level shape and the
  standard ``prompt_tokens_details.cached_tokens`` shape, mirroring the
  Kimi For Coding provider.
- **Vision**: automatic via the OpenAI content-blocks shape on
  ``moonshot-v1-*-vision-preview`` models; no provider-side change needed.
- **Partial mode** and **response_format**: pass-through via the base
  class — callers set them in message content / kwargs respectively.

Reference: https://platform.moonshot.ai/docs/api/chat
"""
from __future__ import annotations

import os
from typing import Any, ClassVar

from butterfly.core.types import TokenUsage
from butterfly.llm_engine.errors import AuthError
from butterfly.llm_engine.providers.openai_api import (
    OpenAIProvider,
    _extract_usage_from_obj,
)


# International endpoint. CN users can override via ``base_url`` arg or the
# ``MOONSHOT_BASE_URL`` env var — e.g. ``https://api.moonshot.cn/v1``.
_MOONSHOT_DEFAULT_BASE_URL = "https://api.moonshot.ai/v1"

# Environment variables, in priority order: an explicit ``api_key`` arg
# beats both, and ``MOONSHOT_API_KEY`` (Moonshot's canonical name) beats
# the ``KIMI_API_KEY`` alias.
_KIMI_API_ENV_VARS: tuple[str, ...] = ("MOONSHOT_API_KEY", "KIMI_API_KEY")
_KIMI_BASE_URL_ENV_VAR = "MOONSHOT_BASE_URL"


def _resolve_kimi_api_api_key(explicit: str | None) -> str:
    """Resolve a Kimi public-API key from explicit arg → env vars.

    Accepts both ``MOONSHOT_API_KEY`` (canonical) and ``KIMI_API_KEY`` (alias);
    the canonical name wins when both are set. Fails fast on missing key so
    the SDK doesn't raise an opaque "auth method unresolved" error later.
    """
    if explicit:
        return explicit
    for var in _KIMI_API_ENV_VARS:
        val = os.environ.get(var)
        if val:
            return val
    raise AuthError(
        "KimiProvider requires one of "
        f"{', '.join(_KIMI_API_ENV_VARS)} to be set, or an explicit api_key argument.",
        provider="kimi",
        status=401,
    )


def _resolve_kimi_base_url(explicit: str | None) -> str:
    """Resolve the Kimi API base URL: explicit → env var → international default."""
    return explicit or os.environ.get(_KIMI_BASE_URL_ENV_VAR) or _MOONSHOT_DEFAULT_BASE_URL


class KimiProvider(OpenAIProvider):
    """Kimi (Moonshot) standalone API — OpenAI-compatible chat completions.

    Points at Moonshot's public API (``api.moonshot.ai`` by default) and
    enables Kimi-specific features on top of the OpenAI Chat Completions
    wire shape. Use this when you have a Moonshot API key purchased from
    ``platform.moonshot.ai``. For the Kimi For Coding plan (separate
    credential, separate gateway), use ``KimiOpenAIProvider`` from
    ``butterfly.llm_engine.providers.kimi``.

    The only model registered in the catalog today is ``kimi-k2.6`` —
    per founder directive, the older ``kimi-k2.5`` / ``kimi-k2-thinking``
    / ``moonshot-v1-*`` families are considered outdated and
    deliberately omitted to keep the surface small. The provider itself
    accepts any model string the Moonshot backend supports, but the
    catalog (and therefore the web-UI dropdown) shows only k2.6.

    Parameters
    ----------
    api_key
        Explicit API key. If omitted, falls back to ``MOONSHOT_API_KEY``
        then ``KIMI_API_KEY``. Raises :class:`AuthError` when none is set.
    base_url
        Custom API base URL. Defaults to ``MOONSHOT_BASE_URL`` env var,
        then ``https://api.moonshot.ai/v1``. Pass the ``.cn`` URL for the
        China region.
    max_tokens
        Max completion tokens forwarded as ``max_tokens`` on non-reasoning
        models, ``max_completion_tokens`` on reasoning ones.
    max_retries
        Forwarded to the underlying ``openai`` SDK client.
    prompt_cache_key
        Optional opaque string that participates in Moonshot's server-side
        prompt cache keying. When set, every request includes
        ``prompt_cache_key=<value>``; cached-token accounting surfaces in
        ``TokenUsage.cache_read_tokens``.
    thinking_keep
        How Kimi treats historical ``reasoning_content`` on multi-turn
        thinking requests. Defaults to ``"all"`` — preserve reasoning across
        turns so the echoed-back blocks this provider already emits get
        used by the server. Set to ``None`` to let Kimi default to "ignore
        history" (rarely what you want when thinking is on).
    """

    # Thinking is enabled via ``extra_body`` rather than the Anthropic-style
    # ``betas`` header. Cache_control is not applicable to Chat Completions
    # (the OpenAI surface has no per-block cache_control shape).
    _supports_thinking: ClassVar[bool] = True

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 8096,
        max_retries: int = 3,
        *,
        prompt_cache_key: str | None = None,
        thinking_keep: str | None = "all",
    ) -> None:
        resolved_key = _resolve_kimi_api_api_key(api_key)
        resolved_base = _resolve_kimi_base_url(base_url)
        super().__init__(
            api_key=resolved_key,
            base_url=resolved_base,
            max_tokens=max_tokens,
            max_retries=max_retries,
        )
        self._prompt_cache_key = prompt_cache_key
        # Normalize to None when the string is falsy so callers can pass
        # empty string / None interchangeably to mean "don't send keep".
        self._thinking_keep = thinking_keep or None

    # ------------------------------------------------------------------
    # Subclass extension hooks
    # ------------------------------------------------------------------

    def _extra_body_for_thinking(
        self,
        *,
        thinking: bool,
        thinking_effort: str,
        thinking_budget: int,
    ) -> dict[str, Any] | None:
        """Inject Kimi's thinking payload plus the optional prompt_cache_key.

        Kimi's thinking API does not expose ``budget_tokens`` or
        ``reasoning_effort`` on Chat Completions — the only knob is the
        binary enable flag plus ``keep`` for multi-turn behavior. The
        effort/budget arguments are accepted (the base class always passes
        them) and deliberately ignored.
        """
        body: dict[str, Any] = {}
        if thinking:
            thinking_payload: dict[str, Any] = {"type": "enabled"}
            if self._thinking_keep is not None:
                thinking_payload["keep"] = self._thinking_keep
            body["thinking"] = thinking_payload
        if self._prompt_cache_key:
            body["prompt_cache_key"] = self._prompt_cache_key
        return body or None

    @staticmethod
    def _extract_usage(usage: Any) -> TokenUsage:
        """Kimi-aware usage extractor.

        Moonshot surfaces cached tokens in two places depending on the
        deployment; prefer the standard ``prompt_tokens_details.cached_tokens``
        and fall back to the top-level ``cached_tokens`` attribute so
        either shape works. Reasoning tokens come from
        ``completion_tokens_details.reasoning_tokens`` when populated
        (thinking-family models).
        """
        base = _extract_usage_from_obj(usage)
        if base.cache_read_tokens > 0:
            return base

        top_cached = getattr(usage, "cached_tokens", 0) or 0
        if top_cached <= 0:
            return base

        non_cached = max(base.input_tokens - top_cached, 0)
        return TokenUsage(
            input_tokens=non_cached,
            output_tokens=base.output_tokens,
            cache_read_tokens=top_cached,
            cache_write_tokens=base.cache_write_tokens,
            reasoning_tokens=base.reasoning_tokens,
        )


__all__ = [
    "KimiProvider",
    "_MOONSHOT_DEFAULT_BASE_URL",
    "_KIMI_API_ENV_VARS",
    "_KIMI_BASE_URL_ENV_VAR",
]
