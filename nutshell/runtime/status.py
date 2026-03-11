from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

DEFAULT_SESSION_STATUS = {
    "model_state": "idle",
    "model_source": "system",
    "updated_at": None,
}


def status_path(session_dir: Path) -> Path:
    return session_dir / "status.json"


def read_session_status(session_dir: Path) -> dict:
    path = status_path(session_dir)
    if not path.exists():
        return dict(DEFAULT_SESSION_STATUS)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_SESSION_STATUS)
    return {
        "model_state": payload.get("model_state", "idle"),
        "model_source": payload.get("model_source", "system"),
        "updated_at": payload.get("updated_at"),
    }


def write_session_status(session_dir: Path, *, model_state: str, model_source: str) -> None:
    payload = {
        "model_state": model_state,
        "model_source": model_source,
        "updated_at": datetime.now().isoformat(),
    }
    status_path(session_dir).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def ensure_session_status(session_dir: Path) -> None:
    path = status_path(session_dir)
    if path.exists():
        return
    write_session_status(session_dir, model_state="idle", model_source="system")
