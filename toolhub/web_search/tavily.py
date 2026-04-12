"""Web search using the Tavily Search API.

Requires TAVILY_API_KEY environment variable.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Optional


def _unsupported_filters(
    *,
    country: Optional[str],
    language: Optional[str],
    freshness: Optional[str],
    date_after: Optional[str],
    date_before: Optional[str],
) -> list[str]:
    unsupported: list[str] = []
    if country:
        unsupported.append("country")
    if language:
        unsupported.append("language")
    if freshness:
        unsupported.append("freshness")
    if date_after:
        unsupported.append("date_after")
    if date_before:
        unsupported.append("date_before")
    return unsupported


def _tavily_search_sync(
    query: str,
    count: int,
    country: Optional[str],
    language: Optional[str],
    freshness: Optional[str],
    date_after: Optional[str],
    date_before: Optional[str],
) -> str:
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        return "Error: TAVILY_API_KEY environment variable is not set."
    unsupported = _unsupported_filters(
        country=country,
        language=language,
        freshness=freshness,
        date_after=date_after,
        date_before=date_before,
    )
    if unsupported:
        joined = ", ".join(unsupported)
        return (
            "Error: Tavily web_search currently supports only 'query' and 'count'. "
            f"Unsupported filters: {joined}."
        )

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


async def _tavily_search(
    query: str,
    count: int = 5,
    country: Optional[str] = None,
    language: Optional[str] = None,
    freshness: Optional[str] = None,
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        _tavily_search_sync,
        query, count, country, language, freshness, date_after, date_before,
    )


_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query string."},
        "count": {"type": "number", "description": "Number of results (1-10). Default: 5.", "minimum": 1, "maximum": 10},
        "country": {"type": "string", "description": "2-letter country code (e.g. 'US', 'CN', 'DE')."},
        "language": {"type": "string", "description": "ISO 639-1 language code (e.g. 'en', 'zh-hans')."},
        "freshness": {"type": "string", "description": "Time filter: 'day', 'week', 'month', or 'year'."},
        "date_after": {"type": "string", "description": "Results published after this date (YYYY-MM-DD)."},
        "date_before": {"type": "string", "description": "Results published before this date (YYYY-MM-DD)."},
    },
    "required": ["query"],
}
