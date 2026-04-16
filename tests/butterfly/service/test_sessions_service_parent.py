"""sessions_service.get_session exposes parent_session_id + mode from manifest."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from butterfly.service.sessions_service import get_session


def _manifest(tmp_path: Path, sid: str, **fields) -> Path:
    sys_dir = tmp_path / "_sessions" / sid
    sys_dir.mkdir(parents=True)
    payload = {"session_id": sid, "entity": "agent", "created_at": datetime.now().isoformat()}
    payload.update(fields)
    (sys_dir / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    # Minimal status so get_session doesn't crash on read_session_status path.
    (sys_dir / "status.json").write_text("{}", encoding="utf-8")
    return sys_dir


def test_get_session_includes_parent_and_mode(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sys_dir = tmp_path / "_sessions"
    sessions_dir.mkdir()
    _manifest(tmp_path, "child-a", parent_session_id="root-1", mode="explorer")

    info = get_session("child-a", sessions_dir, sys_dir)
    assert info is not None
    assert info["parent_session_id"] == "root-1"
    assert info["mode"] == "explorer"


def test_get_session_omits_when_manifest_lacks_fields(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sys_dir = tmp_path / "_sessions"
    sessions_dir.mkdir()
    _manifest(tmp_path, "top-a")

    info = get_session("top-a", sessions_dir, sys_dir)
    assert info is not None
    assert info["parent_session_id"] is None
    assert info["mode"] is None
