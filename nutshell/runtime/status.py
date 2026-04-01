from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

# Canonical shape for status.json.
# Static session config lives in manifest.json.
# All runtime/dynamic state lives here.
DEFAULT_SESSION_STATUS: dict[str, Any] = {
    "model_state": "idle",        # "running" | "idle"
    "model_source": "system",     # "user" | "heartbeat" | "system"
    "updated_at": None,
    "last_run_at": None,          # ISO timestamp of last model run completion
    "pid": None,                  # Daemon process PID (int | None)
    "status": "active",           # "active" | "stopped" | "alignment_blocked"
    "heartbeat_interval": None,   # Mirror of params.json heartbeat_interval (for UI/watcher)
}


def status_path(system_dir: Path) -> Path:
    return system_dir / "status.json"


def read_session_status(system_dir: Path) -> dict:
    path = status_path(system_dir)
    if not path.exists():
        return dict(DEFAULT_SESSION_STATUS)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_SESSION_STATUS)
    result = dict(DEFAULT_SESSION_STATUS)
    result.update(payload)
    return result


def write_session_status(system_dir: Path, **updates: Any) -> None:
    """Update specific fields in status.json. Only provided keys are changed.

    Always touches updated_at. Thread-safe only for single-process access
    (relies on OS file write atomicity for small JSON payloads).
    """
    path = status_path(system_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            current: dict = json.loads(path.read_text(encoding="utf-8"))
        else:
            current = dict(DEFAULT_SESSION_STATUS)
        current.update(updates)
        current["updated_at"] = datetime.now().isoformat()
        path.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def pid_alive(pid: int | None) -> bool:
    """Return True if a process with the given PID is currently running."""
    import os
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, PermissionError, ValueError, OSError):
        return False


def ensure_session_status(system_dir: Path) -> None:
    """Write status.json with defaults if it does not yet exist."""
    path = status_path(system_dir)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(DEFAULT_SESSION_STATUS)
    payload["updated_at"] = datetime.now().isoformat()
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
