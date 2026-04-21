"""Provider-native ``file_search`` built-in tool stub.

Executed server-side by OpenAI's Responses API. Butterfly splices the
``{"type": "file_search", ...}`` dict into the provider's ``tools=[]`` list.
``execute()`` raises :class:`NotImplementedError` — local invocation would be
a configuration bug.
"""
from __future__ import annotations

from typing import Any


_BUILTIN_MESSAGE = (
    "This is a provider-native built-in tool — it must be listed in the "
    "agent's config and is executed server-side. Butterfly does not run "
    "`file_search` locally; configure a Responses/Codex provider and list "
    "`file_search` in tools.md. Vector store IDs travel through the tool "
    "schema / builtin_dict, not via LLM-supplied arguments."
)


class FileSearchExecutor:
    """Stub executor — raises on direct invocation (by design).

    The loader reads ``builtin_dict`` to build the provider tool spec. When
    ``vector_store_ids`` is configured at the toolhub level, the loader
    mutates a copy of ``builtin_dict`` to include those IDs; here we default
    to an empty list so unconfigured agents still fail explicitly on the
    server rather than receive an arbitrary default corpus.
    """

    builtin_dict = {"type": "file_search", "vector_store_ids": []}

    async def execute(self, **_: Any) -> str:
        raise NotImplementedError(_BUILTIN_MESSAGE)
