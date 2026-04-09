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
import shutil
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from nutshell.session_engine.session_params import read_session_params, write_session_params
from nutshell.session_engine.session_status import write_session_status
from .sessions import _init_session, _is_meta_session_id, _read_session_info, _sort_sessions

SESSIONS_DIR = Path(__file__).parent.parent.parent / "sessions"
_SYSTEM_SESSIONS_DIR = Path(__file__).parent.parent.parent / "_sessions"
_DEFAULT_ENTITY = "entity/agent"
_DEFAULT_PORT = 8080

_HTML = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


def _sse_format(event: dict, seq: int | None = None) -> str:
    """Format a server-sent event.

    Includes an 'id:' line when seq is provided, enabling browsers to
    send Last-Event-ID on reconnect. The seq number is a monotonic integer
    over the combined (context + events) stream — not a file byte offset.
    Clients pass it back as ?events_seq=N so the server can skip already-seen
    events rather than relying on fragile byte offsets.
    """
    etype = event.get("type", "message")
    data = json.dumps(event, ensure_ascii=False)
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


def _validate_task_schedule(starts_at: str | None, ends_at: str | None) -> None:
    if starts_at and ends_at and datetime.fromisoformat(ends_at) < datetime.fromisoformat(starts_at):
        raise HTTPException(400, "ends_at must be after starts_at")


def _normalize_task_name(value, field_name: str = "Task name") -> str:
    name = str(value or "").strip()
    if not name:
        raise HTTPException(400, f"{field_name} is required")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise HTTPException(400, f"{field_name} contains invalid characters")
    return name


def _parse_task_status(value) -> str:
    status = str(value or "").strip() or "pending"
    if status not in {"pending", "running", "completed", "paused"}:
        raise HTTPException(400, "Task status must be one of pending, running, completed, paused")
    return status


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

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return _HTML

    @app.get("/api/sessions")
    async def list_sessions():
        if not system_sessions_dir.exists():
            return []
        result = []
        for d in system_sessions_dir.iterdir():
            if not d.is_dir():
                continue
            info = _read_session_info(sessions_dir / d.name, d)
            if info is not None:
                result.append(info)
        return _sort_sessions(result)

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        system_dir = system_sessions_dir / session_id
        session_dir = sessions_dir / session_id
        if not system_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")
        info = _read_session_info(session_dir, system_dir)
        if info is None:
            raise HTTPException(404, f"Session not found: {session_id}")
        params = read_session_params(session_dir)
        params_view = {**params, "is_meta_session": _is_meta_session_id(session_id)}
        return {
            **info,
            "params": params_view,
        }

    @app.post("/api/sessions")
    async def create_session(body: dict):
        session_id = body.get("id") or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        entity = body.get("entity", _DEFAULT_ENTITY)
        heartbeat = float(body.get("heartbeat", 600.0))
        _init_session(sessions_dir, system_sessions_dir, session_id, entity, heartbeat)
        return {"id": session_id, "entity": entity}

    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, body: dict):
        from nutshell.runtime.bridge import BridgeSession
        system_dir = system_sessions_dir / session_id
        if not system_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")
        if _is_meta_session_id(session_id):
            raise HTTPException(403, "Direct chat with meta sessions is disabled.")
        msg_id = BridgeSession(system_dir).send_message(body.get("content", ""))
        return {"id": msg_id}

    @app.post("/api/sessions/{session_id}/interrupt")
    async def interrupt_session_handler(session_id: str):
        """Send a soft interrupt to the session.

        Drains any pending queued inputs and defers the next heartbeat tick.
        In-progress turns run to completion.
        """
        from nutshell.runtime.bridge import BridgeSession
        system_dir = system_sessions_dir / session_id
        if not system_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")
        BridgeSession(system_dir).send_interrupt()
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
        system_dir = system_sessions_dir / session_id
        if not system_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")

        async def generator() -> AsyncIterator[str]:
            from nutshell.runtime.bridge import BridgeSession
            bridge = BridgeSession(system_dir)
            seq = 0
            async for event, _ctx, _evt in bridge.async_iter_events(
                context_offset=context_since,
                events_offset=events_since,
                poll_interval=0.3,
            ):
                yield _sse_format(event, seq=seq)
                seq += 1

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/sessions/{session_id}/history")
    async def get_history(session_id: str):
        from nutshell.runtime.ipc import FileIPC
        ipc = FileIPC(system_sessions_dir / session_id)
        events: list[dict] = []
        context_offset = 0
        for event, off in ipc.tail_history(0):
            events.append(event)
            context_offset = off
        return {
            "events": events,
            "context_offset": context_offset,
            "events_offset": ipc.events_size(),
        }

    @app.post("/api/sessions/{session_id}/stop")
    async def stop_session(session_id: str):
        from nutshell.runtime.ipc import FileIPC
        system_dir = system_sessions_dir / session_id
        if not system_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")
        write_session_status(system_dir, status="stopped", stopped_at=datetime.now().isoformat())
        FileIPC(system_dir).append_event(
            {"type": "status", "value": "heartbeat paused — use ▶ Start to resume"}
        )
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/start")
    async def start_session(session_id: str):
        from nutshell.runtime.ipc import FileIPC
        system_dir = system_sessions_dir / session_id
        if not system_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")
        write_session_status(system_dir, status="active", stopped_at=None)
        FileIPC(system_dir).append_event({"type": "status", "value": "heartbeat resumed"})
        return {"ok": True}

    @app.get("/api/sessions/{session_id}/tasks")
    async def get_tasks(session_id: str):
        from nutshell.session_engine.task_cards import load_all_cards, migrate_legacy_task_sources
        session_dir = sessions_dir / session_id
        if session_dir.exists():
            migrate_legacy_task_sources(session_dir)
        tasks_dir = session_dir / "core" / "tasks"
        cards = sorted(
            load_all_cards(tasks_dir),
            key=lambda c: (c.name != "heartbeat", c.name.lower()),
        )
        return {"cards": [
            {
                "name": c.name,
                "content": c.content,
                "interval": c.interval,
                "starts_at": c.starts_at,
                "ends_at": c.ends_at,
                "status": c.status,
                "last_run_at": c.last_run_at,
                "created_at": c.created_at,
            }
            for c in cards
        ]}

    @app.put("/api/sessions/{session_id}/tasks")
    async def set_tasks(session_id: str, body: dict):
        from nutshell.session_engine.task_cards import TaskCard, delete_card, load_card, migrate_legacy_task_sources, save_card
        session_dir = sessions_dir / session_id
        if not session_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")
        migrate_legacy_task_sources(session_dir)
        tasks_dir = session_dir / "core" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        if "name" in body:
            name = _normalize_task_name(body["name"])
            previous_name = _normalize_task_name(body.get("previous_name") or name, "Previous task name")
            existing = load_card(tasks_dir, previous_name)
            interval = _parse_task_interval(body.get("interval", existing.interval if existing else None))
            starts_at = _parse_task_timestamp(body.get("starts_at", existing.starts_at if existing else None), "starts_at")
            ends_at = _parse_task_timestamp(body.get("ends_at", existing.ends_at if existing else None), "ends_at")
            _validate_task_schedule(starts_at, ends_at)
            if name == "heartbeat" and interval is None:
                interval = float(read_session_params(session_dir).get("heartbeat_interval") or 600.0)
            card = TaskCard(
                name=name,
                content=body.get("content", existing.content if existing else ""),
                interval=interval,
                starts_at=starts_at,
                ends_at=ends_at,
                status=_parse_task_status(body.get("status", existing.status if existing else "pending")),
                last_run_at=body.get("last_run_at", existing.last_run_at if existing else None),
                created_at=body.get("created_at", existing.created_at if existing else datetime.now().isoformat()),
            )
            if previous_name != name:
                delete_card(tasks_dir, previous_name)
            save_card(tasks_dir, card)
            if name == "heartbeat" and card.interval is not None:
                write_session_params(session_dir, heartbeat_interval=card.interval, default_task=None)
        elif "content" in body:
            card = TaskCard(name="task", content=body["content"])
            save_card(tasks_dir, card)
        return {"ok": True}

    @app.delete("/api/sessions/{session_id}/tasks/{task_name}")
    async def remove_task(session_id: str, task_name: str):
        from nutshell.session_engine.task_cards import delete_card, migrate_legacy_task_sources
        session_dir = sessions_dir / session_id
        if not session_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")
        migrate_legacy_task_sources(session_dir)
        deleted = delete_card(session_dir / "core" / "tasks", _normalize_task_name(task_name, "Task name"))
        if not deleted:
            raise HTTPException(404, f"Task not found: {task_name}")
        return {"ok": True}

    @app.get("/api/sessions/{session_id}/config")
    async def get_config(session_id: str):
        session_dir = sessions_dir / session_id
        system_dir = system_sessions_dir / session_id
        if not system_dir.exists() or not session_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")
        params = read_session_params(session_dir)
        return {"params": {**params, "is_meta_session": _is_meta_session_id(session_id)}}

    @app.put("/api/sessions/{session_id}/config")
    async def set_config(session_id: str, body: dict):
        from nutshell.session_engine.task_cards import ensure_heartbeat_card, load_card, migrate_legacy_task_sources, save_card
        session_dir = sessions_dir / session_id
        system_dir = system_sessions_dir / session_id
        if not system_dir.exists() or not session_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")
        params = body.get("params")
        if not isinstance(params, dict):
            raise HTTPException(400, "Body must include a JSON object in 'params'")
        params = dict(params)
        params.pop("is_meta_session", None)
        migrate_legacy_task_sources(session_dir)
        if "default_task" in params:
            heartbeat_content = params.pop("default_task")
            if heartbeat_content not in (None, ""):
                existing_heartbeat = load_card(session_dir / "core" / "tasks", "heartbeat")
                if existing_heartbeat is None:
                    ensure_heartbeat_card(
                        session_dir / "core" / "tasks",
                        interval=float(params.get("heartbeat_interval") or read_session_params(session_dir).get("heartbeat_interval") or 600.0),
                        content=str(heartbeat_content),
                    )
                else:
                    existing_heartbeat.content = str(heartbeat_content)
                    save_card(session_dir / "core" / "tasks", existing_heartbeat)
        if "heartbeat_interval" in params:
            interval = _parse_task_interval(params["heartbeat_interval"])
            if interval is not None:
                heartbeat = load_card(session_dir / "core" / "tasks", "heartbeat")
                if heartbeat is not None:
                    heartbeat.interval = interval
                    save_card(session_dir / "core" / "tasks", heartbeat)
                elif params.get("session_type") == "persistent":
                    ensure_heartbeat_card(session_dir / "core" / "tasks", interval=interval)
        write_session_params(session_dir, **params)
        saved = read_session_params(session_dir)
        return {"ok": True, "params": {**saved, "is_meta_session": _is_meta_session_id(session_id)}}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        system_dir = system_sessions_dir / session_id
        session_dir = sessions_dir / session_id
        if not system_dir.exists() and not session_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")
        write_session_status(system_dir, status="stopped", pid=None, stopped_at=datetime.now().isoformat())
        if session_dir.exists():
            shutil.rmtree(session_dir)
        if system_dir.exists():
            shutil.rmtree(system_dir)
        return {"ok": True}

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
