"""DeepSeek provider (OpenAI- and Anthropic-compatible surfaces).

DeepSeek exposes two equivalent wire surfaces for the same V4 models:

* **OpenAI-compatible** at ``https://api.deepseek.com`` — Chat Completions
  shape; messages carry ``role`` + ``tool_calls``; thinking streams as
  ``delta.reasoning_content``. This is the default and the one most
  callers want.
* **Anthropic-compatible** at ``https://api.deepseek.com/anthropic`` —
  Messages API shape; content is a list of typed blocks
  (``text`` / ``tool_use`` / ``tool_result`` / ``thinking``). Auth is
  ``x-api-key`` via the upstream Anthropic SDK. Useful for callers
  that already depend on Anthropic-shape usage fields
  (``cache_read_input_tokens`` / ``cache_creation_input_tokens``) or
  want to swap DeepSeek in behind existing Anthropic SDK code.

Both surfaces share the same set of models (``deepseek-v4-pro`` /
``deepseek-v4-flash``) and return identical content; they differ only
in wire contract.

Shared provider contract (both classes)
---------------------------------------
* API key resolution: explicit kwarg → ``DEEPSEEK_API_KEY``.
* Base URL defaults pinned to the two DeepSeek hosts; overridable per
  class via a constructor ``base_url`` kwarg (no env override — the
  OpenAI surface supports ``DEEPSEEK_BASE_URL`` for historical
  compatibility, the Anthropic surface does not).
* Thinking mode is enabled via the vendor-specific shape; the caller
  simply toggles ``thinking=True``.

OpenAI-surface specifics
------------------------
Handled in :class:`DeepSeekProvider` below. Thinking enables via
``extra_body={"thinking": {"type": "enabled"}}`` with a
``reasoning_effort`` knob (``high`` | ``max``). Prompt cache hit/miss
is read from DeepSeek's top-level ``prompt_cache_hit_tokens``.

Anthropic-surface specifics
---------------------------
Handled in :class:`DeepSeekAnthropicProvider`. Notable upstream caveats
(captured as class flags so the base class takes the right branch):

* ``cache_control`` breakpoints are **ignored** by the server. We flip
  ``_supports_cache_control = False`` so no ``cache_control`` blocks
  leak into the request body, avoiding wasted tokens on a server
  that discards them anyway.
* ``anthropic-beta`` / ``anthropic-version`` headers are ignored. We
  flip ``_thinking_uses_betas = False`` so the base doesn't send the
  dated interleaved-thinking header, and routes the thinking payload
  through the ``extra_body`` shape DeepSeek accepts.
* Adaptive thinking shape is not recognised — flip
  ``_supports_adaptive_thinking = False`` to force the legacy
  ``{type: "enabled"}`` branch.
* ``budget_tokens`` inside the thinking block is silently ignored by
  the server; we still pass it (no harm) so the legacy branch stays
  uniform across providers.

**Scope — this is from founder's opinion**
    Only the DeepSeek V4 family (``deepseek-v4-pro`` /
    ``deepseek-v4-flash``) is supported. Legacy ``deepseek-chat`` /
    ``deepseek-reasoner`` aliases are intentionally out of scope.

Environment
-----------
``DEEPSEEK_API_KEY``    — bearer token (required by both surfaces).
``DEEPSEEK_BASE_URL``   — optional OpenAI-surface base override. The
                          Anthropic surface ignores this env var by
                          design; override via constructor kwarg if
                          needed.

References
----------
* https://api-docs.deepseek.com/api/create-chat-completion
* https://api-docs.deepseek.com/guides/thinking_mode
* https://api-docs.deepseek.com/guides/function_calling
* https://api-docs.deepseek.com/guides/anthropic_api
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


# Default public endpoints. The OpenAI SDK appends ``chat/completions`` to its
# base URL and the Anthropic SDK appends ``/v1/messages``, so we stop at the
# host / path prefix the respective SDK expects.
_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"

# Efforts accepted by DeepSeek's thinking mode (OpenAI surface). ``max`` is
# documented as the auto-applied value for long agent tasks; ``high`` is the
# interactive default.
_DEEPSEEK_EFFORTS: frozenset[str] = frozenset({"high", "max"})


def _resolve_deepseek_api_key(explicit: str | None) -> str:
    """Resolve a DeepSeek API key — explicit argument wins over env.

    Fails fast with :class:`AuthError` so the SDK doesn't surface an opaque
    "auth method unresolved" error at first-request time.
    """
    resolved = explicit or os.environ.get("DEEPSEEK_API_KEY")
    if not resolved:
        raise AuthError(
            "DeepSeekProvider requires DEEPSEEK_API_KEY to be set, "
            "or an explicit api_key argument.",
            provider="deepseek",
            status=401,
        )
    return resolved


class DeepSeekProvider(OpenAIProvider):
    """LLM provider for the DeepSeek V4 Chat Completions API.

    Thin subclass of :class:`OpenAIProvider` — DeepSeek's public surface is
    OpenAI-compatible so all message-building, tool-encoding, streaming and
    error-mapping logic is inherited verbatim. We layer on:

    * API key resolution from ``DEEPSEEK_API_KEY`` (explicit kwarg wins).
    * Base URL pinned to ``https://api.deepseek.com`` (overridable via
      ``DEEPSEEK_BASE_URL`` for on-prem / gateway deployments).
    * Thinking mode enablement via ``extra_body`` with the ``reasoning_effort``
      knob the V4 family exposes.
    * Usage extraction that reads DeepSeek's top-level
      ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens`` when the
      standard ``prompt_tokens_details.cached_tokens`` slot is absent.

    **Supported models (this is from founder's opinion)**
        Only ``deepseek-v4-pro`` and ``deepseek-v4-flash``. Legacy
        ``deepseek-chat`` / ``deepseek-reasoner`` aliases are deliberately
        out of scope — see the module docstring for the rationale.
    """

    # DeepSeek fully supports thinking mode; the base class picks this up so
    # the CLI/TUI thinking-visibility flow works without extra wiring.
    _supports_thinking: ClassVar[bool] = True

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 8096,
        max_retries: int = 3,
    ) -> None:
        resolved_key = _resolve_deepseek_api_key(api_key)
        resolved_base = (
            base_url
            or os.environ.get("DEEPSEEK_BASE_URL")
            or _DEEPSEEK_BASE_URL
        )
        super().__init__(
            api_key=resolved_key,
            base_url=resolved_base,
            max_tokens=max_tokens,
            max_retries=max_retries,
        )

    # ------------------------------------------------------------------
    # Base-class extension points
    # ------------------------------------------------------------------

    def _extra_body_for_thinking(
        self,
        *,
        thinking: bool,
        thinking_effort: str,
        thinking_budget: int,  # DeepSeek has no token budget knob; ignored.
    ) -> dict[str, Any] | None:
        """Build the ``extra_body`` payload that toggles DeepSeek thinking.

        DeepSeek documents thinking mode as on-by-default for V4 models but
        accepts an explicit ``{"thinking": {"type": "enabled"}}`` payload and
        an optional ``reasoning_effort`` knob (``high`` / ``max``). We emit
        both when ``thinking=True`` so the behaviour matches what the caller
        asked for even if upstream flips the default, and omit the block
        entirely when the caller opted out — that is how the V4-pro /
        v4-flash models honor the non-thinking code path.
        """
        if not thinking:
            return None
        effort = (thinking_effort or "high").strip().lower()
        if effort not in _DEEPSEEK_EFFORTS:
            # Map the general-purpose effort hints (``medium``/``low``/``xhigh``)
            # onto DeepSeek's two-level scale so the agent's thinking_effort
            # field is honored wherever possible. Unknown strings fall back to
            # ``high`` — the interactive default — to keep the request valid.
            effort = "max" if effort in {"xhigh", "highest"} else "high"
        return {
            "thinking": {"type": "enabled"},
            "reasoning_effort": effort,
        }

    @staticmethod
    def _extract_usage(usage: Any) -> TokenUsage:
        """DeepSeek-aware usage extractor.

        Precedence:
          1. If ``prompt_tokens_details.cached_tokens`` is populated, defer to
             the base extractor — DeepSeek sometimes populates the OpenAI-
             shaped field on its V4 responses and that slot is authoritative
             when present.
          2. Otherwise, fall back to DeepSeek's top-level fields
             ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens`` and
             rebuild a TokenUsage that preserves the invariant
             ``input_tokens + cache_read_tokens == prompt_tokens``.
        """
        base = _extract_usage_from_obj(usage)
        if base.cache_read_tokens > 0:
            return base

        cache_hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
        if cache_hit <= 0:
            return base

        # Rebuild using the top-level fields so the caller sees a consistent
        # ``input + cache_read == prompt_tokens`` split. ``max(..., 0)``
        # guards against the (unlikely) case where the cache hit number
        # exceeds the reported prompt total.
        non_cached = max(base.input_tokens - cache_hit, 0)
        return TokenUsage(
            input_tokens=non_cached,
            output_tokens=base.output_tokens,
            cache_read_tokens=cache_hit,
            cache_write_tokens=base.cache_write_tokens,
            reasoning_tokens=base.reasoning_tokens,
        )


class DeepSeekAnthropicProvider(AnthropicProvider):
    """LLM provider for DeepSeek's Anthropic-compatible surface.

    Thin subclass of :class:`AnthropicProvider` pointing at DeepSeek's
    ``/anthropic`` endpoint. The upstream Anthropic SDK is used verbatim
    (``anthropic.AsyncAnthropic(base_url=..., api_key=...)``); we only
    flip class-level capability flags to match what DeepSeek's gateway
    actually honours:

    * ``_supports_cache_control = False`` — DeepSeek explicitly documents
      ``cache_control`` as *ignored* on every content variant, so emitting
      breakpoints wastes tokens and confuses the reader. The base class's
      ``_apply_cache_strategy`` helpers degrade gracefully when this flag
      is False, producing no-cache-breakpoint payloads.
    * ``_thinking_uses_betas = False`` — DeepSeek ignores
      ``anthropic-beta`` / ``anthropic-version`` headers. The base routes
      the thinking payload through the vendor-agnostic ``extra_body``
      shape when this flag is False, matching what DeepSeek accepts.
    * ``_supports_adaptive_thinking = False`` — adaptive thinking is an
      Anthropic-proper feature (4.6+ server-side depth picking). The
      DeepSeek server does not recognise the ``{type: "adaptive"}``
      request shape, so we force the legacy ``enabled + budget_tokens``
      branch even if a ModelSpec lists ``thinking_mode: adaptive``.
      ``budget_tokens`` is silently ignored by the server (documented)
      but we still emit it so the legacy branch stays uniform.

    **Supported models (founder's opinion)**
        Only ``deepseek-v4-pro`` and ``deepseek-v4-flash``.
    """

    _supports_cache_control: ClassVar[bool] = False
    _supports_thinking: ClassVar[bool] = True
    _thinking_uses_betas: ClassVar[bool] = False
    _supports_adaptive_thinking: ClassVar[bool] = False

    def __init__(
        self,
        api_key: str | None = None,
        max_tokens: int = 8096,
        base_url: str | None = None,
    ) -> None:
        resolved_key = _resolve_deepseek_api_key(api_key)
        resolved_base = base_url or _DEEPSEEK_ANTHROPIC_BASE_URL
        super().__init__(
            api_key=resolved_key,
            max_tokens=max_tokens,
            base_url=resolved_base,
        )


__all__ = [
    "DeepSeekProvider",
    "DeepSeekAnthropicProvider",
    "_DEEPSEEK_BASE_URL",
    "_DEEPSEEK_ANTHROPIC_BASE_URL",
    "_DEEPSEEK_EFFORTS",
]
