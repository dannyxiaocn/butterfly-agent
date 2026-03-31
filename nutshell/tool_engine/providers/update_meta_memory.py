
from __future__ import annotations

import json
import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


async def update_meta_memory(
    *,
    content: str,
    layer: str | None = None,
    reason: str,
    _sessions_base: Path | None = None,
) -> str:
    """Persist a memory update to the entity's meta-session."""
    session_id = os.environ.get("NUTSHELL_SESSION_ID")
    if not session_id:
        return "Error: NUTSHELL_SESSION_ID is not set; cannot resolve current session/entity for meta memory update."

    manifest_path = _REPO_ROOT / "_sessions" / session_id / "manifest.json"
    if not manifest_path.exists():
        return f"Error: session manifest not found for session {session_id!r} at {manifest_path}."

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"Error: failed to read session manifest for {session_id!r}: {exc}"

    entity_name = manifest.get("entity")
    if not entity_name:
        return f"Error: manifest for session {session_id!r} does not contain an entity name."

    sessions_base = _sessions_base or (_REPO_ROOT / "sessions")
    meta_dir = sessions_base / f"{entity_name}_meta"
    core_dir = meta_dir / "core"
    (core_dir / "memory").mkdir(parents=True, exist_ok=True)
    (core_dir / "tools").mkdir(parents=True, exist_ok=True)
    (core_dir / "skills").mkdir(parents=True, exist_ok=True)
    (meta_dir / "docs").mkdir(parents=True, exist_ok=True)
    (meta_dir / "playground").mkdir(parents=True, exist_ok=True)
    (core_dir / "memory.md").touch(exist_ok=True)

    if layer is None:
        target = core_dir / "memory.md"
    else:
        safe_layer = Path(layer).name
        if safe_layer in {"", ".", ".."}:
            return f"Error: invalid layer name: {layer!r}"
        target = core_dir / "memory" / f"{safe_layer}.md"
        target.parent.mkdir(parents=True, exist_ok=True)

    target.write_text(content, encoding="utf-8")
    return f"Meta memory updated for entity '{entity_name}' at {target}. Reason: {reason}"
