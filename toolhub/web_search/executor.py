"""Web search executor -- delegates to brave or tavily based on provider config."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_DIR = Path(__file__).parent


def _get_search_func(provider: str):
    """Dynamically load the search function from a sibling module."""
    if provider == "tavily":
        spec = importlib.util.spec_from_file_location("tavily", _DIR / "tavily.py")
        func_name = "_tavily_search"
    else:
        spec = importlib.util.spec_from_file_location("brave", _DIR / "brave.py")
        func_name = "_brave_search"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, func_name)


class WebSearchExecutor:
    """Unified web search executor that delegates to the configured provider."""

    def __init__(self, provider: str = "brave") -> None:
        self._provider = provider

    async def execute(self, **kwargs: Any) -> str:
        search_func = _get_search_func(self._provider)
        return await search_func(**kwargs)
