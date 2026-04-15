from __future__ import annotations
import os
from typing import ClassVar

from butterfly.llm_engine.errors import AuthError
from butterfly.llm_engine.providers.anthropic import AnthropicProvider

# Moonshot's Kimi Code gateway exposes BOTH an Anthropic-compatible surface
# (``/coding/`` — append ``/v1/messages``) and an OpenAI-compatible surface
# (``/coding/v1/chat/completions``). We use the Anthropic path so we can share
# AnthropicProvider. Reference:
#   https://www.kimi.com/code/docs/en/more/third-party-agents.html
_KIMI_BASE_URL = "https://api.kimi.com/coding/"

# Canonical env var for the Kimi For Coding API key. The provider ONLY supports
# this path — there is no ``KIMI_API_KEY`` / ``MOONSHOT_API_KEY`` fallback and
# no ``KIMI_BASE_URL`` override (contact the maintainers if you need a proxy).
_KIMI_ENV_KEY = "KIMI_FOR_CODING_API_KEY"

# Dashboard URL surfaced by ``butterfly kimi login`` when asking the user to
# paste a key. Kept here so the CLI helper and any future tooling read the
# same source of truth.
_KIMI_DASHBOARD_URL = "https://platform.moonshot.ai/console/api-keys"

# Default model used for the login-helper validation ping. Real sessions pick
# their own model via entity config; this only needs to be a valid Kimi For
# Coding model slug that accepts a 16-token echo request.
_KIMI_DEFAULT_VERIFY_MODEL = "kimi-k2-turbo-preview"


class KimiForCodingProvider(AnthropicProvider):
    """LLM provider backed by Kimi For Coding (Moonshot AI).

    Thin wrapper over AnthropicProvider pointing at Kimi's Anthropic-compatible
    messages API. Reads ``KIMI_FOR_CODING_API_KEY`` — there are no alternate
    env vars or endpoint overrides. Thinking is enabled via
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
    ) -> None:
        resolved_key = api_key or os.environ.get(_KIMI_ENV_KEY)
        # Fail fast instead of letting the Anthropic SDK raise an opaque
        # "Could not resolve authentication method" at first-request time.
        # The practical trigger is an agent falling over to kimi-coding-plan
        # from a failing primary without the env var being set.
        if not resolved_key:
            raise AuthError(
                f"KimiForCodingProvider requires {_KIMI_ENV_KEY} to be set, "
                "or an explicit api_key argument.",
                provider="kimi-coding-plan",
                status=401,
            )
        super().__init__(
            api_key=resolved_key,
            max_tokens=max_tokens,
            base_url=_KIMI_BASE_URL,
        )
