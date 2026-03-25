"""app_notify — built-in tool for managing app notifications.

Apps can post notifications that are injected into the agent's system prompt
via ``core/apps/<app>.md`` files.  This gives agents a persistent, always-visible
channel for status updates, alerts, and inter-app communication — separate from
memory (which the agent controls) and tasks (which drive the heartbeat).

Operations:
    write  — create/overwrite an app notification file
    clear  — remove a single app notification file
    list   — list all current app notification files

Notification files live in ``sessions/<id>/core/apps/<app>.md``.
They are loaded by ``Session._load_session_capabilities()`` and rendered as
an "App Notifications" block in the dynamic portion of the system prompt.
"""
from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_DEFAULT_SESSIONS_BASE = _REPO_ROOT / "sessions"


async def app_notify(
    *,
    action: str,
    app: str = "",
    content: str = "",
    _sessions_base: Path | None = None,
) -> str:
    """Manage app notification files in core/apps/.

    Actions:
        write  — Write (create/overwrite) a notification for <app>.
                 Requires ``app`` and ``content``.
        clear  — Remove the notification file for <app>.
                 Requires ``app``.
        list   — List all current app notification files.

    Args:
        action: One of "write", "clear", "list".
        app: App name (used as filename stem, e.g. "chat" → core/apps/chat.md).
             Required for write and clear.
        content: Markdown content for the notification. Required for write.
    """
    session_id = os.environ.get("NUTSHELL_SESSION_ID", "")
    if not session_id:
        return "Error: no active session (NUTSHELL_SESSION_ID not set)."

    sessions_base = _sessions_base or _DEFAULT_SESSIONS_BASE
    apps_dir = sessions_base / session_id / "core" / "apps"

    action = action.lower().strip()

    if action == "list":
        if not apps_dir.is_dir():
            return "No app notifications (core/apps/ does not exist)."
        files = sorted(apps_dir.glob("*.md"))
        if not files:
            return "No app notifications."
        lines = [f"App notifications ({len(files)}):"]
        for f in files:
            size = len(f.read_text(encoding="utf-8"))
            lines.append(f"  - {f.stem} ({size} chars)")
        return "\n".join(lines)

    if action == "write":
        if not app:
            return "Error: 'app' is required for write action."
        if not content:
            return "Error: 'content' is required for write action."
        # Sanitize app name — allow alphanumeric, dash, underscore only
        safe_app = "".join(c for c in app if c.isalnum() or c in "-_")
        if not safe_app:
            return "Error: invalid app name (must contain alphanumeric characters)."
        apps_dir.mkdir(parents=True, exist_ok=True)
        target = apps_dir / f"{safe_app}.md"
        target.write_text(content, encoding="utf-8")
        return f"Written: core/apps/{safe_app}.md ({len(content)} chars)"

    if action == "clear":
        if not app:
            return "Error: 'app' is required for clear action."
        safe_app = "".join(c for c in app if c.isalnum() or c in "-_")
        if not safe_app:
            return "Error: invalid app name."
        target = apps_dir / f"{safe_app}.md"
        if not target.exists():
            return f"No notification found for app '{safe_app}'."
        target.unlink()
        return f"Cleared: core/apps/{safe_app}.md"

    return f"Error: unknown action '{action}'. Use 'write', 'clear', or 'list'."
