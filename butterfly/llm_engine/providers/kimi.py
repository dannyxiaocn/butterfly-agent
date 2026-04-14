from __future__ import annotations
import os
from typing import ClassVar

from butterfly.llm_engine.providers.anthropic import AnthropicProvider

# Moonshot's Kimi Code gateway exposes BOTH an Anthropic-compatible surface
# (``/coding/`` — append ``/v1/messages``) and an OpenAI-compatible surface
# (``/coding/v1/chat/completions``). We use the Anthropic path so we can share
# AnthropicProvider. Reference:
#   https://www.kimi.com/code/docs/en/more/third-party-agents.html
# The ``KIMI_BASE_URL`` env var lets users override for proxies or alt regions.
_KIMI_BASE_URL = "https://api.kimi.com/coding/"


class KimiForCodingProvider(AnthropicProvider):
    """LLM provider backed by Kimi For Coding (Moonshot AI).

    Thin wrapper over AnthropicProvider pointing at Kimi's Anthropic-compatible
    messages API. Reads ``KIMI_FOR_CODING_API_KEY`` by default, with
    ``KIMI_API_KEY`` as a fallback. Thinking is enabled via
    ``extra_body={"thinking": {"type": "enabled"}}`` — Kimi does NOT accept
    Anthropic's betas header mechanism, and the ``thinking`` payload has no
    ``budget_tokens`` field.
    """

    _supports_cache_control: ClassVar[bool] = False
    _supports_thinking: ClassVar[bool] = True
    _thinking_uses_betas: ClassVar[bool] = False

    def __init__(
        self,
        api_key: str | None = None,
        max_tokens: int = 8096,
        base_url: str | None = None,
    ) -> None:
        super().__init__(
            api_key=(
                api_key
                or os.environ.get("KIMI_FOR_CODING_API_KEY")
                or os.environ.get("KIMI_API_KEY")
            ),
            max_tokens=max_tokens,
            base_url=base_url or os.environ.get("KIMI_BASE_URL") or _KIMI_BASE_URL,
        )
