from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_DEFAULT_SESSIONS_BASE = _REPO_ROOT / "sessions"
_DEFAULT_SYSTEM_BASE = _REPO_ROOT / "_sessions"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _preview_text(value, limit: int = 100) -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        text = " ".join(p for p in parts if p)
    else:
        text = str(value or "")
    return text[:limit]


def _read_recent_turns(path: Path) -> list[dict]:
    if not path.exists():
        return []
    turns: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get("type") != "turn":
            continue
        preview = ""
        for msg in reversed(event.get("messages") or []):
            if msg.get("role") == "assistant":
                preview = _preview_text(msg.get("content"))
                break
        if not preview:
            preview = _preview_text(event.get("content"))
        turns.append({
            "triggered_by": event.get("triggered_by"),
            "content_preview": preview,
        })
    return turns[-3:]


async def get_session_info(session_id: str, *, _sessions_base=None, _system_base=None) -> str:
    sessions_base = Path(_sessions_base) if _sessions_base is not None else _DEFAULT_SESSIONS_BASE
    system_base = Path(_system_base) if _system_base is not None else _DEFAULT_SYSTEM_BASE

    manifest = _read_json(system_base / session_id / "manifest.json")
    status = _read_json(system_base / session_id / "status.json")
    tasks_path = sessions_base / session_id / "core" / "tasks.md"
    memory_dir = sessions_base / session_id / "core" / "memory"

    data = {
        "session_id": session_id,
        "entity": manifest.get("entity"),
        "created_at": manifest.get("created_at"),
        "status": status.get("status"),
        "recent_turns": _read_recent_turns(system_base / session_id / "context.jsonl"),
        "tasks": (tasks_path.read_text(encoding="utf-8") if tasks_path.exists() else "")[:500],
        "memory_files": sorted(p.name for p in memory_dir.glob("*.md")) if memory_dir.is_dir() else [],
    }
    return json.dumps(data, ensure_ascii=False)
