"""Built-in tool implementation registry.

ToolLoader checks this registry automatically as a fallback when no
impl_registry entry is found for a tool name. This lets entity configs
declare built-in tools (bash, web_search, etc.) in agent.yaml without
any Python wiring in the server/watcher layer.
"""
from __future__ import annotations
from typing import Callable


def _make_bash() -> Callable:
    from nutshell.runtime.tools.bash import create_bash_tool
    return create_bash_tool()._func


def _make_web_search() -> Callable:
    from nutshell.providers.tool.web_search import create_web_search_tool
    return create_web_search_tool()._func


# Maps tool name → zero-arg factory that returns the callable.
# Lazy factories avoid importing heavy modules at import time.
_BUILTIN_FACTORIES: dict[str, Callable[[], Callable]] = {
    "bash":       _make_bash,
    "web_search": _make_web_search,
}


def get_builtin(name: str) -> Callable | None:
    """Return the implementation callable for a built-in tool, or None."""
    factory = _BUILTIN_FACTORIES.get(name)
    return factory() if factory is not None else None
