"""DeepSeek provider.

DeepSeek's public API is OpenAI-compatible: the Chat Completions endpoint
lives at ``https://api.deepseek.com/chat/completions`` and accepts the same
message / tool / streaming shapes as OpenAI. We therefore subclass
:class:`OpenAIProvider` and only layer on the handful of fields that are
DeepSeek-specific:

* **Thinking mode** — enabled via ``extra_body={"thinking": {"type": "enabled"}}``
  and controllable per-request with ``reasoning_effort`` (``high`` | ``max``).
  Mirrors the Kimi / Moonshot shape.
* **reasoning_content** — the CoT body streams back as
  ``delta.reasoning_content`` and is also present on non-streaming
  ``message.reasoning_content``. The OpenAI base class already captures both
  channels into ``_pending_reasoning_content`` and surfaces the body through
  ``consume_extra_blocks()``, so the agent loop round-trips it to the next
  turn automatically. DeepSeek's reasoner refuses inputs that echo
  ``reasoning_content`` back — see :func:`_scrub_reasoning_for_reasoner`
  below — so we strip it on the reasoner surface and keep it on the V4
  family (which DOES require round-tripping when tool calls are involved).
* **Prompt cache** — DeepSeek reports cache hit/miss at the top level
  (``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``) rather than
  under ``prompt_tokens_details.cached_tokens``. We extend usage extraction
  to read from either shape so downstream accounting stays correct whether
  DeepSeek tweaks the wire format or standardises on the OpenAI-shape.

Environment
-----------
``DEEPSEEK_API_KEY``    — bearer token (required; no legacy fallback).
``DEEPSEEK_BASE_URL``   — optional base URL override for gateways /
                          self-hosted deployments. Defaults to
                          ``https://api.deepseek.com``.

Model identifiers
-----------------
The catalog (``models.yaml``) ships with the DeepSeek V4 family — both the
``deepseek-v4-pro`` top-tier model and the ``deepseek-v4-flash`` cost-
optimised variant. The legacy ``deepseek-chat`` and ``deepseek-reasoner``
aliases keep working (upstream keeps them until 2026-07-24) and route to
the V4-flash non-thinking and thinking modes respectively.

References
----------
* https://api-docs.deepseek.com/api/create-chat-completion
* https://api-docs.deepseek.com/guides/thinking_mode
* https://api-docs.deepseek.com/guides/function_calling
* https://api-docs.deepseek.com/guides/reasoning_model
"""
from __future__ import annotations

import os
import re
from typing import Any, ClassVar

from butterfly.core.types import TokenUsage
from butterfly.llm_engine.errors import AuthError
from butterfly.llm_engine.providers.openai_api import (
    OpenAIProvider,
    _extract_usage_from_obj,
)


# Default public endpoint. The OpenAI SDK appends ``chat/completions`` itself
# so we stop at the host. Kept without the ``/v1`` suffix because DeepSeek's
# gateway rejects the double-prefix some alt clients accidentally produce.
_DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# Efforts accepted by DeepSeek's thinking mode. ``max`` is documented as the
# auto-applied value for long agent tasks; ``high`` is the interactive default.
_DEEPSEEK_EFFORTS: frozenset[str] = frozenset({"high", "max"})

# Reasoner models don't accept tool calls and refuse ``reasoning_content`` on
# inbound messages. The anchor matches the legacy ``deepseek-reasoner`` alias
# exactly; ``deepseek-v4-pro`` / ``-flash`` are treated as general-purpose
# thinking-capable chat models (which DO round-trip reasoning_content with
# tool calls per the V3.2+ thinking-mode guide).
_DEEPSEEK_REASONER_ONLY_RE = re.compile(r"^deepseek-reasoner(?:$|-)", re.IGNORECASE)


def _is_reasoner_only(model: str) -> bool:
    """True when the model is the legacy pure-reasoning variant.

    The legacy ``deepseek-reasoner`` is a thinking-only model with no tool
    support and an explicit ban on echoing ``reasoning_content`` back. Tool
    support was added in the V4 family (``deepseek-v4-pro`` / ``-flash``),
    so only the legacy alias is subject to scrubbing.
    """
    return bool(_DEEPSEEK_REASONER_ONLY_RE.match((model or "").strip()))


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
    """LLM provider for the DeepSeek Chat Completions API.

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
    * Legacy ``deepseek-reasoner`` scrubbing: the reasoner rejects inbound
      ``reasoning_content`` and does not support tool calls, so we strip
      tools + cached reasoning blocks before handing off to the base
      ``complete``.
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
    # complete: scrub legacy reasoner inputs before delegating to base
    # ------------------------------------------------------------------

    async def complete(  # type: ignore[override]
        self,
        messages,
        tools,
        system_prompt,
        model,
        **kwargs: Any,
    ):
        if _is_reasoner_only(model):
            # Reasoner refuses inbound reasoning_content and doesn't support
            # tool calls. Strip both so a mixed history (e.g. upgraded from a
            # V4 session) still round-trips cleanly.
            messages = _scrub_reasoning_for_reasoner(messages)
            tools = []
        return await super().complete(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            model=model,
            **kwargs,
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scrub_reasoning_for_reasoner(messages: list) -> list:
    """Strip ``reasoning_content`` blocks and tool turns for ``deepseek-reasoner``.

    The legacy reasoner returns 400 on any inbound ``reasoning_content`` and
    does not support tool calls at all. We return a shallow copy of each
    assistant message with the offending blocks filtered out so a session
    that previously ran on a tool-capable DeepSeek / Kimi model can be
    replayed on the reasoner without rewriting history.

    Non-assistant messages and plain-string assistant messages are passed
    through untouched to avoid paying the copy cost on every turn.
    """
    from butterfly.core.types import Message  # local import: avoid cycle

    scrubbed: list = []
    for msg in messages:
        if getattr(msg, "role", None) != "assistant":
            scrubbed.append(msg)
            continue
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            scrubbed.append(msg)
            continue
        filtered = [
            block
            for block in content
            if not (
                isinstance(block, dict)
                and block.get("type") in {"reasoning_content", "tool_use"}
            )
        ]
        # If the assistant turn collapses to empty, drop it entirely — the
        # reasoner would otherwise 400 on a content-less turn.
        if not filtered:
            continue
        scrubbed.append(Message(role="assistant", content=filtered))
    return scrubbed


__all__ = [
    "DeepSeekProvider",
    "_DEEPSEEK_BASE_URL",
    "_DEEPSEEK_EFFORTS",
    "_is_reasoner_only",
    "_scrub_reasoning_for_reasoner",
]
