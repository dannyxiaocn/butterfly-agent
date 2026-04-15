from __future__ import annotations
import json
from typing import Any


def _parse_json_args(args_str: str) -> dict[str, Any]:
    """Safely parse a JSON string into a dict; return {} on any failure."""
    if not args_str:
        return {}
    try:
        parsed = json.loads(args_str)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def stringify_tool_result_content(content: Any) -> str:
    """Unified rendering of a tool_result ``content`` payload to plain text.

    The same rule applies across every provider so a given tool_result renders
    identically regardless of which backend receives it:

    * ``str`` → returned verbatim.
    * ``list`` of blocks:
        - ``{"type": "text", "text": ...}`` → text forwarded verbatim.
        - plain string entries → forwarded verbatim.
        - other ``dict`` entries (e.g. ``{"type": "image", ...}``) → replaced
          with ``"[<type> block omitted]"`` so the raw ``dict.__repr__`` never
          leaks into the model's context window.
        - anything else → dropped silently.
    * Any other shape → ``str(content)``.

    ``None`` and empty list collapse to ``""``.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    # Coerce to str so a misbehaving upstream emitting e.g.
                    # ``{"type":"text", "text": 42}`` doesn't TypeError the
                    # downstream ``"".join``.
                    text = block.get("text", "")
                    parts.append(text if isinstance(text, str) else str(text))
                else:
                    btype = block.get("type", "unknown")
                    parts.append(f"[{btype} block omitted]")
            elif isinstance(block, str):
                parts.append(block)
            # drop other shapes silently
        return "".join(parts)
    return str(content)
