"""Nutshell Web UI — FastAPI server with SSE streaming.

Browser connects via SSE to receive real-time agent output; sends messages
via POST. FastAPI is a thin HTTP wrapper over FileIPC — no agent logic here.

Usage:
    nutshell-web
    nutshell-web --port 8080 --sessions-dir ./sessions
    python -m nutshell.ui.web
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

import uvicorn

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from nutshell.service import (
    iter_events as service_iter_events,
    create_session as service_create_session,
    delete_session as service_delete_session,
    get_config as service_get_config,
    get_history as service_get_history,
    get_hud as service_get_hud,
    get_session as service_get_session,
    get_tasks as service_get_tasks,
    interrupt_session as service_interrupt_session,
    is_meta_session as service_is_meta_session,
    list_sessions as service_list_sessions,
    send_message as service_send_message,
    start_session as service_start_session,
    stop_session as service_stop_session,
    update_config as service_update_config,
    upsert_task as service_upsert_task,
    delete_task as service_delete_task,
)
from nutshell.service.sessions_service import _validate_session_id as _service_validate_session_id

SESSIONS_DIR = Path(__file__).parent.parent.parent / "sessions"
_SYSTEM_SESSIONS_DIR = Path(__file__).parent.parent.parent / "_sessions"
_DEFAULT_ENTITY = "entity/agent"
_DEFAULT_PORT = 8080
_DIST_DIR = Path(__file__).parent / "frontend" / "dist"


def _sse_format(event: dict, seq: int | None = None, ctx: int = 0, evt: int = 0) -> str:
    """Format a server-sent event.

    Embeds _ctx/_evt byte offsets into the payload so the client can advance
    its resume offsets on every delivered event, preventing stale-offset
    replay on reconnect (Problem 11).
    """
    etype = event.get("type", "message")
    payload = {**event, "_ctx": ctx, "_evt": evt}
    data = json.dumps(payload, ensure_ascii=False)
    if seq is not None:
        return f"id: {seq}\nevent: {etype}\ndata: {data}\n\n"
    return f"event: {etype}\ndata: {data}\n\n"


def _parse_task_interval(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        interval = float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "Task interval must be a number of seconds")
    if interval < 1:
        raise HTTPException(400, "Task interval must be at least 1 second")
    return interval


def _parse_task_timestamp(value, field_name: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise HTTPException(400, f"{field_name} must be an ISO timestamp string")
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"{field_name} must be a valid ISO timestamp")
    return value


def _validate_task_schedule(start_at: str | None, end_at: str | None) -> None:
    if start_at and end_at and datetime.fromisoformat(end_at) < datetime.fromisoformat(start_at):
        raise HTTPException(400, "end_at must be after start_at")


def _normalize_task_name(value, field_name: str = "Task name") -> str:
    name = str(value or "").strip()
    if not name:
        raise HTTPException(400, f"{field_name} is required")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise HTTPException(400, f"{field_name} contains invalid characters")
    return name


def _parse_task_status(value) -> str:
    status = str(value or "").strip() or "pending"
    if status not in {"pending", "working", "finished", "paused"}:
        raise HTTPException(400, "Task status must be one of pending, working, finished, paused")
    return status


def _raise_session_error(exc: Exception, session_id: str) -> None:
    if isinstance(exc, ValueError):
        raise HTTPException(400, str(exc))
    raise HTTPException(404, f"Session not found: {session_id}")


def _validate_session_id_or_400(session_id: str) -> None:
    try:
        _service_validate_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def create_app(sessions_dir: Path, system_sessions_dir: Path | None = None) -> FastAPI:
    if system_sessions_dir is None:
        system_sessions_dir = sessions_dir.parent / "_sessions"

    from contextlib import asynccontextmanager

    from .weixin import WeixinBridge
    weixin = WeixinBridge(sessions_dir, system_sessions_dir)

    @asynccontextmanager
    async def _lifespan(app):
        weixin.start()
        yield
        weixin.stop()

    app = FastAPI(title="Nutshell Web UI", docs_url=None, redoc_url=None, lifespan=_lifespan)

    # Serve built frontend assets if dist/ exists
    if _DIST_DIR.exists():
        app.mount("/assets", StaticFiles(directory=_DIST_DIR / "assets"), name="assets")

    @app.get("/", response_class=FileResponse)
    async def index():
        return FileResponse(_DIST_DIR / "index.html")

    @app.get("/api/sessions")
    async def list_sessions():
        return service_list_sessions(sessions_dir, system_sessions_dir, exclude_meta=False)

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        try:
            info = service_get_session(session_id, sessions_dir, system_sessions_dir)
            if info is None:
                raise FileNotFoundError(session_id)
            params_view = service_get_config(session_id, sessions_dir, system_sessions_dir)
        except (FileNotFoundError, ValueError) as exc:
            _raise_session_error(exc, session_id)
        return {**info, "params": params_view}

    @app.post("/api/sessions")
    async def create_session(body: dict):
        session_id = body.get("id") or (datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "-" + uuid.uuid4().hex[:4])
        entity = body.get("entity", _DEFAULT_ENTITY)
        try:
            return service_create_session(session_id, entity, sessions_dir=sessions_dir, system_sessions_dir=system_sessions_dir)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, body: dict):
        if service_is_meta_session(session_id):
            raise HTTPException(403, "Direct chat with meta sessions is disabled.")
        try:
            msg_id = service_send_message(session_id, body.get("content", ""), system_sessions_dir)
        except (FileNotFoundError, ValueError) as exc:
            _raise_session_error(exc, session_id)
        return {"id": msg_id}

    @app.post("/api/sessions/{session_id}/interrupt")
    async def interrupt_session_handler(session_id: str):
        """Send a soft interrupt to the session.

        Drains any pending queued inputs and defers the next task tick.
        In-progress turns run to completion.
        """
        try:
            service_interrupt_session(session_id, system_sessions_dir)
        except (FileNotFoundError, ValueError) as exc:
            _raise_session_error(exc, session_id)
        return {"ok": True}

    @app.get("/api/sessions/{session_id}/events")
    async def stream_events(session_id: str, context_since: int = 0, events_since: int = 0):
        """SSE stream of display events for a session.

        Query params:
            context_since  — resume context.jsonl from this byte offset
            events_since   — resume events.jsonl from this byte offset

        Each SSE frame carries an 'id:' line with a monotonic sequence number.
        On reconnect, the browser sends Last-Event-ID automatically; the client
        JS should also pass context_since/events_since from the last /history
        response to avoid replaying the full backlog.

        Event dedup is handled by BridgeSession.async_iter_events(), which
        uses a BoundedIDSet ring buffer to drop re-delivered events.
        """
        _validate_session_id_or_400(session_id)
        system_dir = system_sessions_dir / session_id
        if not system_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")

        async def generator() -> AsyncIterator[str]:
            seq = 0
            async for event, _ctx, _evt in service_iter_events(
                session_id,
                system_sessions_dir,
                context_offset=context_since,
                events_offset=events_since,
                poll_interval=0.3,
            ):
                yield _sse_format(event, seq=seq, ctx=_ctx, evt=_evt)
                seq += 1

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/sessions/{session_id}/history")
    async def get_history(session_id: str, context_since: int = 0):
        try:
            return service_get_history(session_id, system_sessions_dir, context_since=context_since)
        except (FileNotFoundError, ValueError) as exc:
            _raise_session_error(exc, session_id)

    @app.post("/api/sessions/{session_id}/stop")
    async def stop_session(session_id: str):
        try:
            stopped = service_stop_session(session_id, system_sessions_dir)
        except ValueError as exc:
            _raise_session_error(exc, session_id)
        if not stopped:
            raise HTTPException(404, f"Session not found: {session_id}")
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/start")
    async def start_session(session_id: str):
        try:
            started = service_start_session(session_id, system_sessions_dir)
        except ValueError as exc:
            _raise_session_error(exc, session_id)
        if not started:
            raise HTTPException(404, f"Session not found: {session_id}")
        return {"ok": True}

    @app.get("/api/sessions/{session_id}/tasks")
    async def get_tasks(session_id: str):
        try:
            return {"cards": service_get_tasks(session_id, sessions_dir)}
        except ValueError as exc:
            _raise_session_error(exc, session_id)


    @app.put("/api/sessions/{session_id}/tasks")
    async def set_tasks(session_id: str, body: dict):
        payload = dict(body)
        if "name" in payload:
            payload["name"] = _normalize_task_name(payload["name"])
            payload["previous_name"] = _normalize_task_name(payload.get("previous_name") or payload["name"], "Previous task name")
            if "interval" in payload:
                payload["interval"] = _parse_task_interval(payload.get("interval"))
            # Normalize frontend field names → backend canonical names
            if "starts_at" in payload:
                payload["start_at"] = _parse_task_timestamp(payload.pop("starts_at"), "start_at")
            elif "start_at" in payload:
                payload["start_at"] = _parse_task_timestamp(payload.get("start_at"), "start_at")
            if "ends_at" in payload:
                payload["end_at"] = _parse_task_timestamp(payload.pop("ends_at"), "end_at")
            elif "end_at" in payload:
                payload["end_at"] = _parse_task_timestamp(payload.get("end_at"), "end_at")
            if "content" in payload:
                payload["description"] = payload.pop("content")
            _validate_task_schedule(payload.get("start_at"), payload.get("end_at"))
            if "status" in payload:
                payload["status"] = _parse_task_status(payload.get("status"))
        try:
            updated = service_upsert_task(session_id, sessions_dir, **payload)
        except FileExistsError as exc:
            raise HTTPException(409, f"Task '{exc.args[0]}' already exists; choose a different name")
        except ValueError as exc:
            _raise_session_error(exc, session_id)
        if not updated:
            raise HTTPException(404, f"Session not found: {session_id}")
        return {"ok": True}

    @app.delete("/api/sessions/{session_id}/tasks/{task_name}")
    async def remove_task(session_id: str, task_name: str):
        normalized = _normalize_task_name(task_name, "Task name")
        try:
            deleted = service_delete_task(session_id, normalized, sessions_dir)
        except ValueError as exc:
            _raise_session_error(exc, session_id)
        if not deleted:
            raise HTTPException(404, f"Task not found: {task_name}")
        return {"ok": True}

    @app.get("/api/sessions/{session_id}/config")
    async def get_config(session_id: str):
        try:
            return {"params": service_get_config(session_id, sessions_dir, system_sessions_dir)}
        except (FileNotFoundError, ValueError) as exc:
            _raise_session_error(exc, session_id)


    @app.put("/api/sessions/{session_id}/config")
    async def set_config(session_id: str, body: dict):
        params = body.get("params")
        if not isinstance(params, dict):
            raise HTTPException(400, "Body must include a JSON object in 'params'")
        try:
            saved = service_update_config(session_id, sessions_dir, system_sessions_dir, params)
        except (FileNotFoundError, ValueError) as exc:
            _raise_session_error(exc, session_id)
        return {"ok": True, "params": saved}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        try:
            deleted = service_delete_session(session_id, sessions_dir, system_sessions_dir)
        except ValueError as exc:
            _raise_session_error(exc, session_id)
        if not deleted:
            raise HTTPException(404, f"Session not found: {session_id}")
        return {"ok": True}

    @app.get("/api/sessions/{session_id}/hud")
    async def get_session_hud(session_id: str):
        try:
            return service_get_hud(session_id, sessions_dir, system_sessions_dir)
        except (FileNotFoundError, ValueError) as exc:
            _raise_session_error(exc, session_id)

    @app.get("/api/weixin/status")
    async def weixin_status():
        return {
            "status": weixin.status,
            "error": weixin.error,
            "session": weixin._current_session,
            "account": weixin._account_id,
        }

    return app


def main() -> None:
    from nutshell.runtime.env import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser(description="Nutshell Web UI")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--sessions-dir", default=str(SESSIONS_DIR), metavar="DIR")
    parser.add_argument("--system-sessions-dir", default=str(_SYSTEM_SESSIONS_DIR), metavar="DIR")
    args = parser.parse_args()

    sessions_dir = Path(args.sessions_dir)
    system_sessions_dir = Path(args.system_sessions_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    system_sessions_dir.mkdir(parents=True, exist_ok=True)

    app = create_app(sessions_dir, system_sessions_dir)
    print(f"nutshell web UI: http://localhost:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
