"""Web search executor backed by the Tavily Search API."""
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any


def _tavily_search_sync(query: str, count: int) -> str:
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        return "Error: TAVILY_API_KEY environment variable is not set."

    payload: dict = {
        "api_key": api_key,
        "query": query,
        "max_results": min(max(1, count), 10),
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }

    url = "https://api.tavily.com/search"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            response = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return f"Error: Tavily API returned HTTP {e.code}: {body[:500]}"
    except Exception as e:
        return f"Error: {e}"

    results = response.get("results") or []
    if not results:
        return "No results found."

    lines: list[str] = []
    for i, r in enumerate(results[:count], 1):
        title = r.get("title") or "(no title)"
        url_str = r.get("url") or ""
        content = r.get("content") or ""
        published = r.get("published_date") or ""
        lines.append(f"{i}. {title}")
        if url_str:
            lines.append(f"   {url_str}")
        lines.append(f"   {('[' + published + '] ') if published else ''}{content}")
        lines.append("")

    return "\n".join(lines).rstrip()


_SUPPORTED_KEYS = frozenset({"query", "count"})


class WebSearchTavilyExecutor:
    async def execute(
        self,
        query: str,
        count: int | float = 5,
        **extra: Any,
    ) -> str:
        # Tavily only accepts query + count. Reject unknown kwargs loudly so
        # callers that forgot we don't support country/language/freshness get
        # an explicit error instead of silent filter-drop.
        unknown = sorted(k for k in extra if k not in _SUPPORTED_KEYS)
        if unknown:
            return (
                "Error: web_search_tavily only supports 'query' and 'count'. "
                f"Unsupported arguments: {', '.join(unknown)}."
            )
        count_int = int(count)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _tavily_search_sync, query, count_int)
