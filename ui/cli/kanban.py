"""nutshell kanban — unified task-board view across all sessions."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ui.cli.friends import classify_status


# ── Public API ────────────────────────────────────────────────────────────────

def build_kanban(
    sessions: list[dict[str, Any]],
    sessions_base: Path,
) -> list[dict[str, Any]]:
    """Build a kanban entry for every session: id, entity, status, tasks_content."""
    entries: list[dict[str, Any]] = []
    for s in sessions:
        sid = s.get("id", "?")
        entity = s.get("entity", "?")
        status = classify_status(s)

        tasks_path = sessions_base / sid / "core" / "tasks.md"
        if tasks_path.exists():
            content = tasks_path.read_text(encoding="utf-8").strip()
        else:
            content = ""

        entries.append({
            "id": sid,
            "entity": entity,
            "status": status,
            "tasks_content": content,
        })
    return entries


def format_kanban_table(entries: list[dict[str, Any]]) -> str:
    """Pretty-print the kanban board."""
    if not entries:
        return "No sessions found."

    _STATUS_DOT = {
        "online": "●",
        "idle": "◐",
        "offline": "○",
    }

    blocks: list[str] = []
    for e in entries:
        dot = _STATUS_DOT.get(e["status"], "?")
        header = f"{dot} {e['entity']}  ({e['id']})  [{e['status']}]"
        content = e["tasks_content"] if e["tasks_content"] else "(empty)"
        # Indent task content
        indented = "\n".join(f"  {line}" for line in content.splitlines())
        blocks.append(f"{header}\n{indented}")
    return "\n\n".join(blocks)


def format_kanban_json(entries: list[dict[str, Any]]) -> str:
    """JSON output for machine consumption."""
    return json.dumps(entries, ensure_ascii=False, indent=2)
