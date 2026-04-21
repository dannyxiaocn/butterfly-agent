"""Provider-native ``web_search`` built-in tool stub.

This tool is executed server-side by OpenAI's Responses API (Codex OAuth or
OpenAI Responses provider). Butterfly never calls ``execute()`` directly —
the loader recognises the built-in flag and splices ``{"type": "web_search"}``
into the provider's ``tools=[]`` list. Calling ``execute()`` raises
:class:`NotImplementedError` to surface a configuration bug early.
"""
from __future__ import annotations

from typing import Any


_BUILTIN_MESSAGE = (
    "This is a provider-native built-in tool — it must be listed in the "
    "agent's config and is executed server-side. Butterfly does not run "
    "`web_search` locally; configure a Responses/Codex provider and list "
    "`web_search` in tools.md."
)


class WebSearchExecutor:
    """Stub executor — raises on direct invocation (by design)."""

    # Surfaces the raw provider-tool dict the agent loop splices into the
    # provider's ``tools=[]`` list. Non-empty return ⇒ Tool.is_builtin.
    builtin_dict = {"type": "web_search"}

    async def execute(self, **_: Any) -> str:
        raise NotImplementedError(_BUILTIN_MESSAGE)
