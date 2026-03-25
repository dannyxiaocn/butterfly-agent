from __future__ import annotations
import os
from typing import ClassVar

from nutshell.llm_engine.providers.anthropic import AnthropicProvider

_KIMI_BASE_URL = "https://api.kimi.com/coding/"


class KimiForCodingProvider(AnthropicProvider):
    """LLM provider backed by Kimi For Coding (Moonshot AI).

    Thin wrapper over AnthropicProvider pointing at Kimi's Anthropic-compatible
    messages API. Uses KIMI_FOR_CODING_API_KEY env var by default, with
    KIMI_API_KEY kept as a compatibility fallback.
    """

    # Kimi's API does not support Anthropic cache_control blocks.
    _supports_cache_control: ClassVar[bool] = False

    def __init__(
        self,
        api_key: str | None = None,
        max_tokens: int = 8096,
        base_url: str = _KIMI_BASE_URL,
    ) -> None:
        super().__init__(
            api_key=api_key or os.environ.get("KIMI_FOR_CODING_API_KEY") or os.environ.get("KIMI_API_KEY"),
            max_tokens=max_tokens,
            base_url=base_url,
        )
