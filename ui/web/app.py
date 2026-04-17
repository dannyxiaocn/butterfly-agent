"""Butterfly Web UI — FastAPI server with SSE streaming.

Browser connects via SSE to receive real-time agent output; sends messages
via POST. FastAPI is a thin HTTP wrapper over FileIPC — no agent logic here.

Not exposed as a console script. v2.0.16 dropped `butterfly-web` from
pyproject.toml; `butterfly` (no args, in `ui/cli/main.py::cmd_default`)
calls `create_app()` and runs uvicorn in-process. `main()` below stays
for `python -m ui.web.app` use cases (scripts / direct invocation).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

import uvicorn

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from butterfly.service import (
    iter_events as service_iter_events,
    create_session as service_create_session,
    delete_session as service_delete_session,
    get_config as service_get_config,
    get_config_yaml as service_get_config_yaml,
    get_history as service_get_history,
    get_hud as service_get_hud,
    get_models_catalog as service_get_models_catalog,
    get_session as service_get_session,
    get_tasks as service_get_tasks,
    interrupt_session as service_interrupt_session,
    is_meta_session as service_is_meta_session,
    list_sessions as service_list_sessions,
    send_message as service_send_message,
    start_session as service_start_session,
    stop_session as service_stop_session,
    update_config as service_update_config,
    update_config_yaml as service_update_config_yaml,
    upsert_task as service_upsert_task,
    delete_task as service_delete_task,
)
from butterfly.service.sessions_service import _validate_session_id as _service_validate_session_id

SESSIONS_DIR = Path(__file__).parent.parent.parent / "sessions"
_SYSTEM_SESSIONS_DIR = Path(__file__).parent.parent.parent / "_sessions"
_DEFAULT_AGENT = "agenthub/agent"
_DEFAULT_PORT = 7720
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

    app = FastAPI(title="Butterfly Web UI", docs_url=None, redoc_url=None, lifespan=_lifespan)

    # Serve built frontend assets if dist/ exists
    if _DIST_DIR.exists():
        app.mount("/assets", StaticFiles(directory=_DIST_DIR / "assets"), name="assets")

    @app.get("/", response_class=FileResponse)
    async def index():
        return FileResponse(_DIST_DIR / "index.html")

    @app.get("/api/update_status")
    async def update_status():
        """Return the auto-update worker's latest status, if any.

        Shape (written by `butterfly/runtime/server.py::_auto_update_worker`):
          - `{applied: true, new_head, applied_at, reload: true}` after a
            silent update landed; the frontend force-reloads on seeing a
            newer `applied_at` than its last-seen value.
          - `{available: true, dirty: true, commits_behind, ...}` when the
            worker sees upstream commits but the tree is dirty — frontend
            shows a top-right notification.
          - `{}` when no pending update.
        """
        status_path = system_sessions_dir / "update_status.json"
        if not status_path.exists():
            return {}
        try:
            return json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

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
        agent = body.get("agent", _DEFAULT_AGENT)
        try:
            return service_create_session(session_id, agent, sessions_dir=sessions_dir, system_sessions_dir=system_sessions_dir)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, body: dict):
        if service_is_meta_session(session_id):
            raise HTTPException(403, "Direct chat with meta sessions is disabled.")
        # mode: "interrupt" (default — cancel in-flight + merge if uncommitted)
        # or "wait" (queue + merge with the trailing wait-mode chat input).
        mode = body.get("mode", "interrupt")
        if mode not in ("interrupt", "wait"):
            raise HTTPException(400, f"invalid mode: {mode!r}")
        try:
            msg_id = service_send_message(
                session_id,
                body.get("content", ""),
                system_sessions_dir,
                mode=mode,
            )
        except (FileNotFoundError, ValueError) as exc:
            _raise_session_error(exc, session_id)
        return {"id": msg_id, "mode": mode}

    @app.post("/api/sessions/{session_id}/interrupt")
    async def interrupt_session_handler(session_id: str):
        """Bare interrupt — cancel the in-flight run AND drop the inbox.

        v2.0.12: this is the explicit ⚡ interrupt button. It differs from
        sending a chat with ``mode=interrupt`` (which cancels and runs the
        new content): a bare interrupt just stops, with nothing to run in
        its place.
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

    @app.get("/api/sessions/{session_id}/events_tail")
    async def get_events_tail(session_id: str, n: int = 10):
        """Return the last `n` events from a session's events.jsonl.

        Used by the parent's panel card for a sub-agent child: the parent's
        SSE stream doesn't subscribe to children's event streams (each session
        is its own SSE source), so we read the file directly when the user
        expands the sub_agent card. Cheap — events.jsonl is bounded by the
        child's lifetime and we only pull the last few.
        """
        _validate_session_id_or_400(session_id)
        n = max(1, min(int(n), 100))
        system_dir = system_sessions_dir / session_id
        events_path = system_dir / "events.jsonl"
        if not events_path.exists():
            return []
        import json as _json
        try:
            with events_path.open("r", encoding="utf-8") as f:
                lines = f.readlines()[-n:]
        except OSError:
            return []
        out: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(_json.loads(line))
            except _json.JSONDecodeError:
                continue
        return out

    @app.get("/api/sessions/{session_id}/panel")
    async def get_panel(session_id: str):
        """List all panel entries for a session, sorted by created_at asc.

        Returns [] if the panel dir doesn't yet exist (session may not have
        spawned any backgroundable tool calls). 404 only if the session itself
        is missing.
        """
        _validate_session_id_or_400(session_id)
        session_dir = sessions_dir / session_id
        if not session_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")
        from butterfly.session_engine.panel import list_entries
        panel_dir = session_dir / "core" / "panel"
        entries = list_entries(panel_dir)
        return [e.to_json() for e in entries]

    @app.get("/api/sessions/{session_id}/panel/{tid}")
    async def get_panel_entry(session_id: str, tid: str):
        """Return a single panel entry + the last 40 lines of its output_file."""
        _validate_session_id_or_400(session_id)
        session_dir = sessions_dir / session_id
        if not session_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")
        from butterfly.session_engine.panel import load_entry
        panel_dir = session_dir / "core" / "panel"
        entry = load_entry(panel_dir, tid)
        if entry is None:
            raise HTTPException(404, f"Panel entry not found: {tid}")
        tail: str | None = None
        if entry.output_file:
            # output_file is stored as a path relative to the repo root (see
            # design.md §5.1); resolve relative paths against the repo root,
            # which is the sessions_dir's parent.
            candidate = Path(entry.output_file)
            if not candidate.is_absolute():
                candidate = sessions_dir.parent / candidate
            try:
                if candidate.exists():
                    # Stream + bounded deque instead of whole-file load, so a
                    # multi-GB log doesn't materialise in RAM just to extract
                    # a 40-line tail.
                    from collections import deque
                    buf: "deque[str]" = deque(maxlen=40)
                    with candidate.open("r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            buf.append(line.rstrip("\n"))
                    tail = "\n".join(buf)
            except OSError as exc:
                console_msg = f"[panel] could not read output_file {candidate}: {exc}"
                print(console_msg)
                tail = None
        payload = entry.to_json()
        payload["output_tail"] = tail
        return payload

    @app.post("/api/sessions/{session_id}/panel/{tid}/kill")
    async def kill_panel_entry(session_id: str, tid: str):
        """Mark a panel entry as killed at the file level.

        Does NOT send a signal to the underlying process; the background
        task manager reaps on its next tick. 404 if the entry is missing.
        """
        _validate_session_id_or_400(session_id)
        session_dir = sessions_dir / session_id
        if not session_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")
        from butterfly.session_engine.panel import (
            STATUS_KILLED,
            load_entry,
            save_entry,
        )
        panel_dir = session_dir / "core" / "panel"
        entry = load_entry(panel_dir, tid)
        if entry is None:
            raise HTTPException(404, f"Panel entry not found: {tid}")
        entry.status = STATUS_KILLED
        entry.finished_at = time.time()
        save_entry(panel_dir, entry)
        return {"status": "killed"}

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

    @app.get("/api/sessions/{session_id}/config/yaml")
    async def get_config_yaml(session_id: str):
        """Return raw YAML text of the session's config.yaml.

        Web UI uses this for the raw-YAML editor tab so it can round-trip the
        on-disk file byte-for-byte (comments stripped by PyYAML, but field
        order and quoting semantics preserved).
        """
        try:
            text = service_get_config_yaml(session_id, sessions_dir, system_sessions_dir)
        except (FileNotFoundError, ValueError) as exc:
            _raise_session_error(exc, session_id)
        return {"yaml": text}

    @app.put("/api/sessions/{session_id}/config/yaml")
    async def set_config_yaml(session_id: str, body: dict):
        text = body.get("yaml")
        if not isinstance(text, str):
            raise HTTPException(400, "Body must include 'yaml' string")
        try:
            saved = service_update_config_yaml(session_id, sessions_dir, system_sessions_dir, text)
        except (FileNotFoundError,) as exc:
            _raise_session_error(exc, session_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "params": saved}

    @app.get("/api/models")
    async def get_models():
        """Return the provider → models catalog used by the config editor."""
        return service_get_models_catalog()

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
    from butterfly.runtime.env import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser(description="Butterfly Web UI")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT, help="HTTP port (default: %(default)s)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--sessions-dir", default=str(SESSIONS_DIR), metavar="DIR")
    parser.add_argument("--system-sessions-dir", default=str(_SYSTEM_SESSIONS_DIR), metavar="DIR")
    args = parser.parse_args()

    sessions_dir = Path(args.sessions_dir)
    system_sessions_dir = Path(args.system_sessions_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    system_sessions_dir.mkdir(parents=True, exist_ok=True)

    app = create_app(sessions_dir, system_sessions_dir)
    print(f"butterfly web UI: http://localhost:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
