from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_DEFAULT_SESSIONS_BASE = _REPO_ROOT / "sessions"
_DEFAULT_SYSTEM_BASE = _REPO_ROOT / "_sessions"
_DEFAULT_ARCHIVED_BASE = _REPO_ROOT / "_archived"


def _safe_load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


async def archive_session(
    session_id: str,
    reason: str = "",
    *,
    _sessions_base: Path | None = None,
    _system_base: Path | None = None,
    _archived_base: Path | None = None,
) -> str:
    sessions_base = _sessions_base if _sessions_base is not None else _DEFAULT_SESSIONS_BASE
    system_base = _system_base if _system_base is not None else _DEFAULT_SYSTEM_BASE
    archived_base = _archived_base if _archived_base is not None else _DEFAULT_ARCHIVED_BASE

    src_session = sessions_base / session_id
    src_system = system_base / session_id
    dst_session = archived_base / "sessions" / session_id
    dst_system = archived_base / "_sessions" / session_id

    if not src_session.exists() and not src_system.exists():
        return f"Error: session '{session_id}' not found."

    try:
        manifest = _safe_load_json(src_system / "manifest.json") if src_system.exists() else {}
        entity_name = manifest.get("entity", "")
        audit_src = src_session / "core" / "audit.jsonl"
        if audit_src.exists() and entity_name:
            meta_audit_dir = system_base / f"{entity_name}_meta" / "core" / "audit"
            meta_audit_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(audit_src, meta_audit_dir / f"{session_id}.jsonl")

        if src_session.exists():
            dst_session.parent.mkdir(parents=True, exist_ok=True)
            if dst_session.exists():
                shutil.rmtree(dst_session)
            shutil.move(str(src_session), str(dst_session))

        if src_system.exists():
            dst_system.parent.mkdir(parents=True, exist_ok=True)
            if dst_system.exists():
                shutil.rmtree(dst_system)
            shutil.move(str(src_system), str(dst_system))
        elif dst_session.exists():
            manifest = _safe_load_json(archived_base / "_sessions" / session_id / "manifest.json")

        archive_info = {
            "archived_at": datetime.now().isoformat(),
            "reason": reason,
            "entity": manifest.get("entity", ""),
        }
        if dst_system.exists():
            (dst_system / "archive_info.json").write_text(json.dumps(archive_info, ensure_ascii=False), encoding="utf-8")
        elif dst_session.exists():
            (dst_session / "archive_info.json").write_text(json.dumps(archive_info, ensure_ascii=False), encoding="utf-8")
        return f"archived {session_id}"
    except Exception as exc:
        return f"Error archiving session '{session_id}': {exc}"
