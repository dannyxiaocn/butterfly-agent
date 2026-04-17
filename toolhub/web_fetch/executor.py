"""Web fetch executor — loads the sibling httpx.py by path."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_DIR = Path(__file__).parent


def _load_fetcher_cls():
    spec = importlib.util.spec_from_file_location("httpx_fetch", _DIR / "httpx.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.HttpxFetcher


class WebFetchExecutor:
    def __init__(self) -> None:
        cls = _load_fetcher_cls()
        self._fetcher = cls()

    async def execute(self, **kwargs: Any) -> str:
        return await self._fetcher.execute(**kwargs)
