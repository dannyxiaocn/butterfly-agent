"""nutshell visit — agent room view for a single session."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON file; return {} on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fmt_ago(ts: str | None) -> str:
    """Format ISO timestamp as 'Xm ago' etc., or '—'."""
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = int((datetime.now(tz=timezone.utc) - dt).total_seconds())
        if secs < 0:
            return "just now"
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return "—"


def _read_recent_context(context_path: Path, n: int = 3) -> list[dict[str, Any]]:
    """Read the last *n* user_input + turn pairs from context.jsonl.

    Returns a list of dicts with keys: type, summary (str, ≤80 chars), ts.
    """
    if not context_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        for raw in context_path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = obj.get("type")
            if etype == "user_input":
                content = obj.get("content", "")
                entries.append({
                    "type": "user_input",
                    "summary": content[:80],
                    "ts": obj.get("ts"),
                })
            elif etype == "turn":
                # Extract last assistant message content
                msgs = obj.get("messages", [])
                text = ""
                for m in reversed(msgs):
                    if m.get("role") == "assistant":
                        c = m.get("content", "")
                        if isinstance(c, str):
                            text = c
                        elif isinstance(c, list):
                            # content blocks
                            parts = []
                            for block in c:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    parts.append(block.get("text", ""))
                                elif isinstance(block, str):
                                    parts.append(block)
                            text = " ".join(parts)
                        break
                entries.append({
                    "type": "turn",
                    "summary": text[:80],
                    "ts": obj.get("ts"),
                })
    except Exception:
        return []
    return entries[-n:]


def _read_tasks(tasks_path: Path) -> str:
    """Read tasks.md content, return stripped text or empty string."""
    try:
        return tasks_path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _read_apps(apps_dir: Path) -> dict[str, str]:
    """Read all .md files from core/apps/ directory."""
    result: dict[str, str] = {}
    if not apps_dir.is_dir():
        return result
    for f in sorted(apps_dir.iterdir()):
        if f.suffix == ".md" and f.is_file():
            try:
                result[f.stem] = f.read_text(encoding="utf-8").strip()
            except Exception:
                pass
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def gather_room_data(
    session_id: str,
    *,
    sessions_base: Path,
    system_base: Path,
) -> dict[str, Any]:
    """Gather all data for the agent room view.

    Returns a dict with keys:
      id, entity, created_at, status, model_state, last_run_at,
      recent_context, tasks, apps
    """
    sys_dir = system_base / session_id
    sess_dir = sessions_base / session_id

    # manifest
    manifest = _read_json(sys_dir / "manifest.json")
    # status
    status_data = _read_json(sys_dir / "status.json")
    # context
    recent = _read_recent_context(sys_dir / "context.jsonl")
    # tasks
    tasks = _read_tasks(sess_dir / "core" / "tasks.md")
    # apps
    apps = _read_apps(sess_dir / "core" / "apps")

    return {
        "id": session_id,
        "entity": manifest.get("entity", "?"),
        "created_at": manifest.get("created_at"),
        "status": status_data.get("status", "unknown"),
        "model_state": status_data.get("model_state", "unknown"),
        "last_run_at": status_data.get("last_run_at"),
        "recent_context": recent,
        "tasks": tasks,
        "apps": apps,
    }


def format_room_text(data: dict[str, Any]) -> str:
    """Format room data as human-readable text."""
    lines: list[str] = []

    # Header
    lines.append(f"Session: {data['id']}")
    lines.append(f"Entity:  {data['entity']}")
    lines.append(f"Status:  {data['status']} / {data['model_state']}")
    created = data.get("created_at") or "—"
    last_run = data.get("last_run_at")
    lines.append(f"Created: {created}")
    lines.append(f"Last active: {_fmt_ago(last_run)}")

    # Recent context
    lines.append("")
    lines.append("--- Recent Activity ---")
    recent = data.get("recent_context", [])
    if not recent:
        lines.append("(no activity)")
    else:
        for entry in recent:
            prefix = ">>>" if entry["type"] == "user_input" else "<<<"
            lines.append(f"  {prefix} {entry['summary']}")

    # Tasks
    lines.append("")
    lines.append("--- Task Board ---")
    tasks = data.get("tasks", "")
    if tasks:
        for tl in tasks.splitlines():
            lines.append(f"  {tl}")
    else:
        lines.append("  (empty)")

    # Apps
    apps = data.get("apps", {})
    if apps:
        lines.append("")
        lines.append("--- App Notifications ---")
        for name, content in apps.items():
            lines.append(f"  [{name}]")
            for al in content.splitlines():
                lines.append(f"    {al}")

    return "\n".join(lines)


def format_room_json(data: dict[str, Any]) -> str:
    """Format room data as JSON string."""
    return json.dumps(data, ensure_ascii=False, indent=2)


def cmd_visit(args) -> int:
    """Entry point for 'nutshell visit' subcommand."""
    session_id: str | None = args.session_id
    sessions_base: Path = args.sessions_base
    system_base: Path = args.system_base

    # Resolve session ID (default to latest)
    if not session_id:
        if not system_base.is_dir():
            print("Error: no sessions found", file=__import__("sys").stderr)
            return 1
        dirs = sorted(
            (d.name for d in system_base.iterdir() if d.is_dir()),
            reverse=True,
        )
        if not dirs:
            print("Error: no sessions found", file=__import__("sys").stderr)
            return 1
        session_id = dirs[0]

    # Verify session exists
    sys_dir = system_base / session_id
    if not sys_dir.is_dir():
        print(f"Error: session '{session_id}' not found", file=__import__("sys").stderr)
        return 1

    data = gather_room_data(
        session_id,
        sessions_base=sessions_base,
        system_base=system_base,
    )

    if args.as_json:
        print(format_room_json(data))
    else:
        print(format_room_text(data))
    return 0
