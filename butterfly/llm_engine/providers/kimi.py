"""Kimi (Moonshot) providers.

Moonshot's Kimi Code gateway exposes two surfaces backed by the same models:

- OpenAI-compatible chat completions API at ``/coding/v1/`` (POST
  ``/chat/completions``) — **default**, matches kimi-cli's own default path.
- Anthropic-compatible messages API at ``/coding/`` (POST ``/v1/messages``)
  — opt-in for callers that need Anthropic-shape usage fields.

Both classes are strict about authentication: they resolve the API key from
an explicit argument or ``KIMI_FOR_CODING_API_KEY`` **only**. There is no
legacy ``KIMI_API_KEY`` / ``MOONSHOT_API_KEY`` fallback and no base-URL
override — if a proxy is required, edit the ``_KIMI_*_BASE_URL`` constants
in this module. Keeping auth narrow prevents the "which env var actually
got used?" debugging rabbit hole when a fallback provider quietly picks
up a stale credential.

Pick by the shape of the usage fields you care about:

- ``KimiOpenAIProvider`` (default): returns ``cached_tokens`` and
  ``reasoning_tokens`` in usage. Recommended when you want prompt-cache
  hit-rate metrics or reasoning token accounting.
- ``KimiAnthropicProvider``: returns Anthropic-shape usage
  (``cache_read_input_tokens`` / ``cache_creation_input_tokens``). Thinking
  is enabled server-side but ``cache_control`` is not honored by this
  surface, so we leave it off.

Kimi For Coding requires a ``User-Agent`` header that identifies the client
as an authorized coding agent (kimi-cli, Claude Code, Roo Code, etc.). Both
providers set ``User-Agent: claude-code/0.1.0`` — matching the value used by
openclaw's kimi-coding extension — so that Kimi's access control accepts the
request. Without this header the API returns a 403 ``access_terminated_error``
with the message "Kimi For Coding is currently only available for Coding
Agents".

Reference: https://www.kimi.com/code/docs/en/more/third-party-agents.html
"""
from __future__ import annotations
import os
from typing import Any, ClassVar

from butterfly.core.types import TokenUsage
from butterfly.llm_engine.errors import AuthError
from butterfly.llm_engine.providers.anthropic import AnthropicProvider
from butterfly.llm_engine.providers.openai_api import (
    OpenAIProvider,
    _extract_usage_from_obj,
)


# The Anthropic-compatible surface lives at the root of ``/coding/``; the
# anthropic SDK appends ``/v1/messages`` itself.
_KIMI_ANTHROPIC_BASE_URL = "https://api.kimi.com/coding/"

# The OpenAI-compatible surface lives under ``/coding/v1/``; the openai SDK
# appends ``chat/completions`` itself.
_KIMI_OPENAI_BASE_URL = "https://api.kimi.com/coding/v1/"

# Historical alias — some external code and tests still import
# ``_KIMI_BASE_URL`` by name (the Anthropic-surface constant pre-v2.0.10).
_KIMI_BASE_URL = _KIMI_ANTHROPIC_BASE_URL

# User-Agent header required by Kimi For Coding to identify the client as an
# authorized coding agent. Matches openclaw's kimi-coding extension value.
_KIMI_USER_AGENT = "claude-code/0.1.0"

# Convenience dict injected as ``default_headers`` in both providers.
_KIMI_DEFAULT_HEADERS: dict[str, str] = {"User-Agent": _KIMI_USER_AGENT}


def _resolve_kimi_api_key(explicit: str | None, *, provider_label: str) -> str:
    """Resolve a Kimi API key from explicit arg → ``KIMI_FOR_CODING_API_KEY``.

    Only the Kimi For Coding plan is supported; legacy ``KIMI_API_KEY`` is
    intentionally not accepted, since butterfly only talks to the
    ``/coding/`` gateway. Fail-fast on missing key prevents the SDK from
    raising an opaque "auth method unresolved" error at first-request time.
    """
    resolved = explicit or os.environ.get("KIMI_FOR_CODING_API_KEY")
    if not resolved:
        raise AuthError(
            f"{provider_label} requires KIMI_FOR_CODING_API_KEY to be set, "
            "or an explicit api_key argument.",
            provider="kimi-coding-plan",
            status=401,
        )
    return resolved


class KimiAnthropicProvider(AnthropicProvider):
    """Kimi For Coding via Moonshot's Anthropic-compatible endpoint.

    Thin wrapper over ``AnthropicProvider`` pointing at Kimi's Anthropic-shape
    messages API. Thinking is enabled via
    ``extra_body={"thinking": {"type": "enabled"}}`` — Kimi does NOT accept
    Anthropic's betas header mechanism, and the thinking payload has no
    ``budget_tokens`` field. ``cache_control`` is not honored by this
    surface, so we keep it off.
    """

    _supports_cache_control: ClassVar[bool] = False
    _supports_thinking: ClassVar[bool] = True
    _thinking_uses_betas: ClassVar[bool] = False

    def __init__(
        self,
        api_key: str | None = None,
        max_tokens: int = 8096,
    ) -> None:
        resolved_key = _resolve_kimi_api_key(
            api_key, provider_label="KimiAnthropicProvider"
        )
        super().__init__(
            api_key=resolved_key,
            max_tokens=max_tokens,
            base_url=_KIMI_ANTHROPIC_BASE_URL,
            default_headers=_KIMI_DEFAULT_HEADERS,
        )


class KimiOpenAIProvider(OpenAIProvider):
    """Kimi For Coding via Moonshot's OpenAI-compatible endpoint.

    Matches kimi-cli's default path (``kosong/chat_provider/kimi.py``).
    Thinking is enabled via
    ``extra_body={"thinking": {"type": "enabled"}}``; Kimi's OpenAI surface
    does not expose ``reasoning_effort`` on its own model family, so we
    ignore ``thinking_effort`` / ``thinking_budget`` and only toggle the
    binary enable flag (identical behavior to the Anthropic surface).

    Usage extraction prefers Moonshot's top-level ``cached_tokens``, falling
    back to the standard ``prompt_tokens_details.cached_tokens`` so both
    deployment shapes Just Work. Reasoning tokens come from
    ``completion_tokens_details.reasoning_tokens`` when the backend
    populates it.
    """

    _supports_thinking: ClassVar[bool] = True

    def __init__(
        self,
        api_key: str | None = None,
        max_tokens: int = 8096,
        max_retries: int = 3,
    ) -> None:
        resolved_key = _resolve_kimi_api_key(
            api_key, provider_label="KimiOpenAIProvider"
        )
        super().__init__(
            api_key=resolved_key,
            base_url=_KIMI_OPENAI_BASE_URL,
            max_tokens=max_tokens,
            max_retries=max_retries,
            default_headers=_KIMI_DEFAULT_HEADERS,
        )

    def _extra_body_for_thinking(
        self,
        *,
        thinking: bool,
        thinking_effort: str,
        thinking_budget: int,
    ) -> dict[str, Any] | None:
        if not thinking:
            return None
        return {"thinking": {"type": "enabled"}}

    @staticmethod
    def _extract_usage(usage: Any) -> TokenUsage:
        """Kimi-aware usage extractor.

        Moonshot surfaces cached tokens in two places depending on the
        deployment; prefer the top-level ``cached_tokens`` attribute and
        fall back to the standard ``prompt_tokens_details.cached_tokens``
        so either shape works without a config knob. Mirrors kimi-cli's
        precedence in ``kosong/chat_provider/kimi.py``.
        """
        base = _extract_usage_from_obj(usage)
        if base.cache_read_tokens > 0:
            return base

        top_cached = getattr(usage, "cached_tokens", 0) or 0
        if top_cached <= 0:
            return base

        # _extract_usage_from_obj already subtracted whatever it found in
        # prompt_tokens_details (zero here); subtract the top-level figure
        # from the non-cached input so ``input + cache_read`` still equals
        # the original ``prompt_tokens``.
        non_cached = max(base.input_tokens - top_cached, 0)
        return TokenUsage(
            input_tokens=non_cached,
            output_tokens=base.output_tokens,
            cache_read_tokens=top_cached,
            cache_write_tokens=base.cache_write_tokens,
            reasoning_tokens=base.reasoning_tokens,
        )


# ---------------------------------------------------------------------------
# Back-compat shim
# ---------------------------------------------------------------------------

# Older code (agent configs, live sessions, external callers) imports
# ``KimiForCodingProvider`` directly. The class has been split in two; keep
# the historical name as an alias to the Anthropic variant so existing
# sessions keep working unchanged. Callers that want the new default should
# import ``KimiOpenAIProvider`` explicitly or resolve via the registry key
# ``kimi-coding-plan`` (which now points at the OpenAI variant).
KimiForCodingProvider = KimiAnthropicProvider

__all__ = [
    "KimiAnthropicProvider",
    "KimiOpenAIProvider",
    "KimiForCodingProvider",
    "_KIMI_ANTHROPIC_BASE_URL",
    "_KIMI_OPENAI_BASE_URL",
    "_KIMI_BASE_URL",
    "_KIMI_USER_AGENT",
    "_KIMI_DEFAULT_HEADERS",
]
