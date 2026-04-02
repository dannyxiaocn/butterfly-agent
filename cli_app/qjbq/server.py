"""QjbQ FastAPI server — notification relay for nutshell agents.

Endpoints:
    POST /api/notify                 — write an app notification to a session
    GET  /api/notify/{session_id}    — list all app notifications for a session
    POST /api/session-message        — relay an agent message into a session context
    GET  /health                     — health check
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

from cli_app.qjbq import __version__

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SESSIONS_DIR = _REPO_ROOT / "sessions"
_DEFAULT_SYSTEM_SESSIONS_DIR = _REPO_ROOT / "_sessions"


def _sessions_dir() -> Path:
    env = os.environ.get("QJBQ_SESSIONS_DIR")
    if env:
        return Path(env)
    return _DEFAULT_SESSIONS_DIR


def _system_sessions_dir() -> Path:
    env = os.environ.get("QJBQ_SYSTEM_SESSIONS_DIR")
    if env:
        return Path(env)
    return _DEFAULT_SYSTEM_SESSIONS_DIR


app = FastAPI(title="QjbQ", version=__version__)


class NotifyRequest(BaseModel):
    session_id: str
    app: str
    content: str

    @field_validator("session_id", "app", "content")
    @classmethod
    def not_empty(cls, v: str, info) -> str:
        if not v or not v.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return v.strip()


class NotifyResponse(BaseModel):
    ok: bool
    path: str
    chars: int


class NotificationItem(BaseModel):
    app: str
    content: str
    chars: int


class NotifyListResponse(BaseModel):
    session_id: str
    notifications: list[NotificationItem]


class SessionMessageRequest(BaseModel):
    session_id: str
    content: str
    message_id: str
    caller: str = "agent"
    mode: str = "sync"
    ts: str | None = None

    @field_validator("session_id", "content", "message_id", "caller", "mode")
    @classmethod
    def not_empty(cls, v: str, info) -> str:
        if not v or not v.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return v.strip()

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in {"sync", "async"}:
            raise ValueError("mode must be 'sync' or 'async'")
        return v


class SessionMessageResponse(BaseModel):
    ok: bool
    path: str
    message_id: str


class HealthResponse(BaseModel):
    status: str
    version: str


def _sanitize_app(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in "-_")


def _validate_session_id(session_id: str) -> str:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    if safe != session_id:
        raise HTTPException(status_code=400, detail="Invalid session_id")
    return safe


def _append_jsonl(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


@app.post("/api/notify", response_model=NotifyResponse)
async def post_notify(req: NotifyRequest) -> NotifyResponse:
    session_id = _validate_session_id(req.session_id)
    safe_app = _sanitize_app(req.app)
    if not safe_app:
        raise HTTPException(status_code=400, detail="Invalid app name")

    apps_dir = _sessions_dir() / session_id / "core" / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)

    target = apps_dir / f"{safe_app}.md"
    target.write_text(req.content, encoding="utf-8")

    rel_path = f"sessions/{session_id}/core/apps/{safe_app}.md"
    return NotifyResponse(ok=True, path=rel_path, chars=len(req.content))


@app.get("/api/notify/{session_id}", response_model=NotifyListResponse)
async def get_notifications(session_id: str) -> NotifyListResponse:
    session_id = _validate_session_id(session_id)
    apps_dir = _sessions_dir() / session_id / "core" / "apps"

    notifications: list[NotificationItem] = []
    if apps_dir.is_dir():
        for f in sorted(apps_dir.glob("*.md")):
            content = f.read_text(encoding="utf-8")
            notifications.append(NotificationItem(app=f.stem, content=content, chars=len(content)))

    return NotifyListResponse(session_id=session_id, notifications=notifications)


@app.post("/api/session-message", response_model=SessionMessageResponse)
async def post_session_message(req: SessionMessageRequest) -> SessionMessageResponse:
    session_id = _validate_session_id(req.session_id)
    system_dir = _system_sessions_dir() / session_id
    manifest_path = system_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    context_path = system_dir / "context.jsonl"
    _append_jsonl(
        context_path,
        {
            "type": "user_input",
            "content": req.content,
            "id": req.message_id,
            "caller": req.caller,
            "mode": req.mode,
            "ts": req.ts,
        },
    )
    rel_path = f"_sessions/{session_id}/context.jsonl"
    return SessionMessageResponse(ok=True, path=rel_path, message_id=req.message_id)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)
