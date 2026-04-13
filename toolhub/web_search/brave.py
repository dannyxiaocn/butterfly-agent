"""Web search using the Brave Search API.

Requires BRAVE_API_KEY environment variable.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional


def _brave_search_sync(
    query: str,
    count: int,
    country: Optional[str],
    language: Optional[str],
    freshness: Optional[str],
    date_after: Optional[str],
    date_before: Optional[str],
) -> str:
    api_key = os.environ.get("BRAVE_API_KEY", "").strip()
    if not api_key:
        return "Error: BRAVE_API_KEY environment variable is not set."

    params: dict[str, str | int] = {
        "q": query,
        "count": min(max(1, count), 10),
    }
    if country:
        params["country"] = country.upper()
    if language:
        params["search_lang"] = language.lower()
    if date_after and date_before:
        params["freshness"] = f"{date_after}to{date_before}"
    elif date_after:
        params["freshness"] = f"{date_after}to9999-12-31"
    elif date_before:
        params["freshness"] = f"2000-01-01to{date_before}"
    elif freshness:
        mapping = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}
        params["freshness"] = mapping.get(freshness.lower(), freshness)

    url = f"https://api.search.brave.com/res/v1/web/search?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "X-Subscription-Token": api_key,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return f"Error: Brave Search API returned HTTP {e.code}: {body[:500]}"
    except Exception as e:
        return f"Error: {e}"

    results = (data.get("web") or {}).get("results") or []
    if not results:
        return "No results found."

    lines: list[str] = []
    for i, r in enumerate(results[:count], 1):
        title = r.get("title") or "(no title)"
        url_str = r.get("url") or ""
        desc = r.get("description") or ""
        age = r.get("age") or ""
        lines.append(f"{i}. {title}")
        if url_str:
            lines.append(f"   {url_str}")
        lines.append(f"   {('[' + age + '] ') if age else ''}{desc}")
        lines.append("")

    return "\n".join(lines).rstrip()


async def _brave_search(
    query: str,
    count: int | float = 5,
    country: Optional[str] = None,
    language: Optional[str] = None,
    freshness: Optional[str] = None,
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
) -> str:
    count = int(count)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        _brave_search_sync,
        query, count, country, language, freshness, date_after, date_before,
    )


_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query string."},
        "count": {"type": "integer", "description": "Number of results (1-10). Default: 5.", "minimum": 1, "maximum": 10},
        "country": {"type": "string", "description": "2-letter country code (e.g. 'US', 'CN', 'DE')."},
        "language": {"type": "string", "description": "ISO 639-1 language code (e.g. 'en', 'zh-hans')."},
        "freshness": {"type": "string", "description": "Time filter: 'day', 'week', 'month', or 'year'."},
        "date_after": {"type": "string", "description": "Results published after this date (YYYY-MM-DD)."},
        "date_before": {"type": "string", "description": "Results published before this date (YYYY-MM-DD)."},
    },
    "required": ["query"],
}
