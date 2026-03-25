"""state_diff — built-in tool for token-efficient state tracking.

Maintains a named snapshot in core/state/<key>.txt and returns a unified diff
on subsequent calls. Designed for high-frequency status checks (ps, df, git
status, etc.) where showing only the changed lines is cheaper and more useful
than dumping the full output every time.

Usage pattern:
    output = bash(command="ps aux")
    state_diff(key="ps", content=output)  # first call → initialized
    # ... next heartbeat ...
    output = bash(command="ps aux")
    state_diff(key="ps", content=output)  # shows only what changed
"""
from __future__ import annotations

import difflib
import os
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_DEFAULT_SESSIONS_BASE = _REPO_ROOT / "sessions"


async def state_diff(
    *,
    key: str,
    content: str,
    _sessions_base: Path | None = None,
) -> str:
    """Store a named state snapshot and return a diff vs. the previous snapshot.

    First call: stores the content and returns a summary line.
    Subsequent calls: stores new content and returns a unified diff. If nothing
    changed, returns "(no change)".

    State is stored in core/state/<key>.txt within the current session
    directory. Keys should be short identifiers (e.g. "ps", "disk", "git").

    Args:
        key: Short identifier for this state (e.g. "ps", "disk", "git_status").
        content: New state string to compare against the previous snapshot.
    """
    session_id = os.environ.get("NUTSHELL_SESSION_ID", "")
    if not session_id:
        return "Error: no active session (NUTSHELL_SESSION_ID not set)."

    sessions_base = _sessions_base or _DEFAULT_SESSIONS_BASE
    state_dir = sessions_base / session_id / "core" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / f"{key}.txt"

    if not state_file.exists():
        state_file.write_text(content, encoding="utf-8")
        n = len(content.splitlines())
        return f"(state '{key}' initialized: {n} line{'s' if n != 1 else ''})"

    old = state_file.read_text(encoding="utf-8")
    if old == content:
        return f"(no change: '{key}')"

    diff_lines = list(difflib.unified_diff(
        old.splitlines(keepends=True),
        content.splitlines(keepends=True),
        fromfile=f"{key} (prev)",
        tofile=f"{key} (now)",
        n=2,
    ))
    state_file.write_text(content, encoding="utf-8")
    return "".join(diff_lines) or f"(updated '{key}': content changed but diff is empty)"
