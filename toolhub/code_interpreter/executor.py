"""Provider-native ``code_interpreter`` built-in tool stub.

Executed server-side by OpenAI's Responses API. Butterfly splices the
``{"type": "code_interpreter", ...}`` dict into the provider's ``tools=[]``
list. ``execute()`` raises :class:`NotImplementedError` — local invocation
would be a configuration bug.
"""
from __future__ import annotations

from typing import Any


_BUILTIN_MESSAGE = (
    "This is a provider-native built-in tool — it must be listed in the "
    "agent's config and is executed server-side. Butterfly does not run "
    "`code_interpreter` locally; configure a Responses/Codex provider and "
    "list `code_interpreter` in tools.md. For local Python execution use "
    "`bash` with a python invocation instead."
)


class CodeInterpreterExecutor:
    """Stub executor — raises on direct invocation (by design).

    ``builtin_dict`` defaults to an auto-managed container; override via the
    agent config when a specific container is required.
    """

    builtin_dict = {"type": "code_interpreter", "container": {"type": "auto"}}

    async def execute(self, **_: Any) -> str:
        raise NotImplementedError(_BUILTIN_MESSAGE)
