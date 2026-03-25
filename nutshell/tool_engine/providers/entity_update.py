"""propose_entity_update — built-in tool for agents to request entity file changes.

Usage (agent-facing):
    propose_entity_update(
        file_path="entity/agent/prompts/system.md",
        content="<new full file content>",
        reason="Why this change improves the agent",
    )

The request is written to _entity_updates/ and must be approved by a human
via `nutshell-review-updates` before it takes effect globally.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

_UPDATES_DIR_NAME = "_entity_updates"
_REPO_ROOT = Path(__file__).parent.parent.parent.parent


async def propose_entity_update(
    *,
    file_path: str,
    content: str,
    reason: str,
    _entity_base: Path | None = None,
    _updates_base: Path | None = None,
) -> str:
    """Submit a request to update an entity file. Requires human approval.

    Args:
        file_path: Relative path within the repo (must start with 'entity/').
        content:   Full new content for the file.
        reason:    Why this change is needed.
    """
    entity_base = _entity_base or (_REPO_ROOT / "entity")
    updates_base = _updates_base or (_REPO_ROOT / _UPDATES_DIR_NAME)

    # Security: path must be relative, within entity/, and not contain ..
    path = Path(file_path)
    if path.is_absolute():
        return f"Error: file_path must be relative, got absolute path: {file_path!r}"

    # Resolve against entity_base to detect traversal
    try:
        target = (entity_base / path).resolve()
        entity_base_resolved = entity_base.resolve()
        target.relative_to(entity_base_resolved)
    except ValueError:
        return (
            f"Error: invalid file_path {file_path!r} — must be within entity/ directory. "
            f"Example: 'entity/agent/prompts/system.md'"
        )
    except Exception as exc:
        return f"Error: {exc}"

    session_id = os.environ.get("NUTSHELL_SESSION_ID", "unknown")
    record = {
        "id": str(uuid.uuid4()),
        "ts": datetime.now().isoformat(),
        "session_id": session_id,
        "file_path": str(path),
        "content": content,
        "reason": reason,
        "status": "pending",
    }

    updates_base.mkdir(parents=True, exist_ok=True)
    out_path = updates_base / f"{record['id']}.json"
    out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    return (
        f"Update request submitted (id: {record['id']}).\n"
        f"File: {file_path}\n"
        f"Awaiting human review via `nutshell-review-updates`."
    )
