"""Butterfly Web UI — FastAPI server (Phase 7 rewrite).

Every handler is a thin shell over ``butterfly.runtime.io``. No business
logic lives here; no file access. The web layer is a transport for the
io module (DESIGN.md §7.1 — invariants I5 and I6).

**Protocol bump (Phase 7)**: ``GET /api/sessions/{id}/events/stream`` now
emits one SSE event per ``Event`` in events_v1.jsonl with monotonic ``id:``
fields. The retired event types (``partial_text``, ``tool_done``,
``tool_finalize``, ``agent_output_start``/``done``, …) are gone (§3.6).
The Phase 8 frontend rewrite consumes the new schema; the currently-deployed
frontend WILL break on the SSE stream — this is the intended cutover.

Not exposed as a console script. v2.0.16 dropped ``butterfly-web`` from
pyproject.toml; ``butterfly`` (no args, in ``ui/cli/main.py::cmd_default``)
calls ``create_app()`` and runs uvicorn in-process. ``main()`` below
stays for ``python -m ui.web.app`` use cases.
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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from butterfly.runtime import io

SESSIONS_DIR = Path(__file__).parent.parent.parent / "sessions"
_SYSTEM_SESSIONS_DIR = Path(__file__).parent.parent.parent / "_sessions"
_DEFAULT_AGENT = "agenthub/agent"
_DEFAULT_PORT = 7250
_DIST_DIR = Path(__file__).parent / "frontend" / "dist"

# SSE keep-alive cadence — tail_events() returns after this many idle
# seconds; we then emit a keep-alive comment and re-arm. Keeps load
# balancer / proxy idle timers happy while letting the event loop
# react to ``shutdown_event`` promptly.
_SSE_KEEPALIVE_SECONDS = 15.0


# ── Error mapping ────────────────────────────────────────────────────────────
#
# Every handler follows the same pattern: validate → call io.foo(...) →
# return JSON. We funnel the four expected exception classes into HTTP
# status codes here so each handler body stays 3-5 lines.

def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, str(exc) or "not found")
    if isinstance(exc, FileExistsError):
        return HTTPException(409, str(exc) or "already exists")
    if isinstance(exc, (ValueError, IOError)):
        return HTTPException(400, str(exc) or "bad request")
    if isinstance(exc, NotImplementedError):
        return HTTPException(501, str(exc) or "not implemented")
    raise exc  # 500 via FastAPI


# ── SSE framing ──────────────────────────────────────────────────────────────

def _sse_frame(event_id: int, event_type: str, data: dict) -> str:
    """Format one SSE event per DESIGN.md §7.2.

    ``id:`` field = the Event's monotonic id so the browser's built-in
    reconnect sends ``Last-Event-ID: N`` and we resume from id > N.
    ``event:`` = the type. ``data:`` = the full Event object as JSON
    (one event, one SSE payload — never batched).
    """
    body = json.dumps(data, ensure_ascii=False)
    return f"id: {event_id}\nevent: {event_type}\ndata: {body}\n\n"


# Back-compat export for legacy unit tests that import this helper. The new
# SSE framing is `_sse_frame`; the shim maps seq→id + passes through data.
def _sse_format(event: dict, seq: int | None = None, ctx: int = 0, evt: int = 0) -> str:
    """DEPRECATED: legacy SSE formatter kept for test imports.

    Phase 8 will delete this entirely. The new SSE stream uses
    :func:`_sse_frame` with Event ids as the SSE ``id:`` field.
    """
    etype = event.get("type", "message")
    payload = {**event, "_ctx": ctx, "_evt": evt}
    data = json.dumps(payload, ensure_ascii=False)
    if seq is not None:
        return f"id: {seq}\nevent: {etype}\ndata: {data}\n\n"
    return f"event: {etype}\ndata: {data}\n\n"


# ── App factory ──────────────────────────────────────────────────────────────

def create_app(
    sessions_dir: Path,
    system_sessions_dir: Path | None = None,
    agenthub_dir: Path | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    ``sessions_dir`` / ``system_sessions_dir`` / ``agenthub_dir`` exist
    for test isolation — the real runtime passes the repo-rooted
    constants; tests pass TemporaryDirectory roots. We monkeypatch the
    ``butterfly.runtime.io`` module-level constants so every reader /
    writer resolves against the caller's tmp layout.
    """
    if system_sessions_dir is None:
        system_sessions_dir = sessions_dir.parent / "_sessions"
    if agenthub_dir is None:
        agenthub_dir = Path(__file__).resolve().parent.parent.parent / "agenthub"

    # Swap the io module's repo-rooted defaults for the test-supplied
    # dirs. This is the single knob that makes ``io.list_sessions()``
    # etc. resolve to the right layout without threading the path through
    # every call.
    io._SESSIONS_DIR = sessions_dir  # type: ignore[attr-defined]
    io._SYSTEM_SESSIONS_DIR = system_sessions_dir  # type: ignore[attr-defined]
    io._REPO_ROOT = agenthub_dir.parent  # type: ignore[attr-defined]

    from contextlib import asynccontextmanager

    from .weixin import WeixinBridge
    weixin = WeixinBridge(sessions_dir, system_sessions_dir)

    shutdown_event = asyncio.Event()

    @asynccontextmanager
    async def _lifespan(app):
        weixin.start()
        try:
            yield
        finally:
            shutdown_event.set()
            weixin.stop()

    app = FastAPI(
        title="Butterfly Web UI",
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    if _DIST_DIR.exists():
        app.mount(
            "/assets",
            StaticFiles(directory=_DIST_DIR / "assets"),
            name="assets",
        )

    # ── Static shell ─────────────────────────────────────────────────────

    @app.get("/", response_class=FileResponse)
    async def index():
        return FileResponse(_DIST_DIR / "index.html")

    # ── Session lifecycle ────────────────────────────────────────────────

    @app.get("/api/sessions")
    async def list_sessions():
        # Meta-sessions are included in the response (the sidebar may
        # choose to filter them client-side). Matches the pre-refactor
        # exclude_meta=False contract.
        return io.list_sessions()

    @app.post("/api/sessions")
    async def create_session_endpoint(body: dict):
        # session_id is always server-generated. The old contract accepted
        # a body ``id`` field; dropping it silently would mask stale
        # clients so we surface a 400.
        if "id" in body:
            raise HTTPException(
                400,
                "body field 'id' is no longer accepted; session_id is "
                "always server-generated. Use 'display_name' for a "
                "human-readable label.",
            )
        session_id = (
            datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            + "-"
            + uuid.uuid4().hex[:4]
        )
        agent = body.get("agent", _DEFAULT_AGENT)
        display_name = body.get("display_name")
        try:
            io.create_session(
                session_id,
                agent=agent,
                display_name=display_name,
            )
        except Exception as exc:
            raise _http_error(exc) from exc
        # Preserve the legacy response shape: {"id", "agent", "display_name"}.
        # io.create_session returns the EVENT; we need the session summary.
        try:
            info = io.get_session(session_id)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {
            "id": session_id,
            "agent": info.get("agent", agent),
            "display_name": info.get("display_name"),
        }

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        try:
            info = io.get_session(session_id)
            # The legacy endpoint merged a fresh config payload into the
            # session info (the config includes ``is_meta_session`` while
            # the raw info ``params`` does not). Keep that quirk so the
            # deployed frontend's config dropdown survives.
            params = io.read_config(session_id)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {**info, "params": params}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        try:
            io.delete_session(session_id)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/start")
    async def start_session(session_id: str):
        try:
            io.start_session(session_id)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/stop")
    async def stop_session(session_id: str):
        try:
            io.stop_session(session_id)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"ok": True}

    # ── Input (messages, interrupt) ──────────────────────────────────────

    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, body: dict):
        if str(session_id).endswith("_meta"):
            raise HTTPException(403, "Direct chat with meta sessions is disabled.")
        text = body.get("content", "")
        if not isinstance(text, str):
            raise HTTPException(400, "Body must include 'content' string")
        # ``mode`` ∈ {"interrupt", "wait"}. Passed through to the dispatcher
        # so the user can queue a message behind an in-flight tick (the
        # restored chat input's ⌥+Enter / wait checkbox).
        mode = body.get("mode", "interrupt")
        if mode not in ("interrupt", "wait"):
            raise HTTPException(400, "mode must be 'interrupt' or 'wait'")
        try:
            event = io.send_message(session_id, text, source="web", mode=mode)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"id": event.id, "event_id": event.id, "mode": mode}

    @app.post("/api/sessions/{session_id}/interrupt")
    async def interrupt_session(session_id: str):
        try:
            io.interrupt_session(session_id)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"ok": True}

    # ── Events (replay + live SSE stream) ────────────────────────────────

    @app.get("/api/sessions/{session_id}/events")
    async def list_events(session_id: str, since_id: int = 0):
        """JSON replay. Events are returned newest-last (chronological)."""
        try:
            events = list(io.read_events(session_id, since_id=since_id or None))
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"events": [e.to_dict() for e in events]}

    @app.get("/api/sessions/{session_id}/history")
    async def read_history(session_id: str, since_id: int = 0):
        """Display history — for_llm events + UI-visible system events."""
        try:
            events = io.read_display_history(session_id, since_id=since_id)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"events": [e.to_dict() for e in events]}

    @app.get("/api/sessions/{session_id}/events/stream")
    async def stream_events(
        session_id: str,
        request: Request,
        cursor: int = 0,
    ):
        """SSE live tail (DESIGN.md §7.2).

        One Event = one SSE frame. The ``id:`` field carries the Event
        id so the browser's automatic reconnect (sends ``Last-Event-ID``)
        picks up where it left off. Client reconnects with the header
        override the query param.

        Keep-alive comments fire every ``_SSE_KEEPALIVE_SECONDS`` to
        prevent intermediate proxies / load-balancers from dropping an
        idle stream.
        """
        # Validate session + resume-point.
        try:
            # Touches disk once; raises FileNotFoundError / ValueError.
            io.latest_event_id(session_id)
        except Exception as exc:
            raise _http_error(exc) from exc

        # Last-Event-ID header takes precedence over the ?cursor= param
        # — this is the SSE reconnect contract.
        last_event_id = request.headers.get("last-event-id")
        if last_event_id is not None:
            try:
                cursor = int(last_event_id)
            except ValueError:
                pass

        async def generator() -> AsyncIterator[str]:
            """Re-arm tail_events after each idle timeout.

            The primitive returns when no events land for ``timeout``
            seconds; we reopen it with the updated cursor so the HTTP
            stream stays alive indefinitely. Kick off with a comment so
            HTTP status + headers flush immediately (otherwise chunked
            proxies buffer until the first real frame). Outer ``try``
            catches the generator-close cancellation so we don't
            propagate uvicorn teardown noise.
            """
            yield ": open\n\n"
            resume = cursor
            try:
                while not shutdown_event.is_set():
                    async for ev in io.tail_events(
                        session_id,
                        cursor=resume,
                        timeout=_SSE_KEEPALIVE_SECONDS,
                    ):
                        yield _sse_frame(ev.id, ev.type, ev.to_dict())
                        resume = ev.id
                        if shutdown_event.is_set():
                            return
                    # idle window expired — keep-alive + re-arm.
                    yield ": keepalive\n\n"
            except (asyncio.CancelledError, GeneratorExit):
                return

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ── HUD / tasks / todo / config / prompts / assets ───────────────────

    @app.get("/api/sessions/{session_id}/hud")
    async def read_hud(session_id: str):
        try:
            return io.read_hud(session_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/sessions/{session_id}/tasks")
    async def read_tasks(session_id: str):
        try:
            return {"cards": io.read_task_cards(session_id)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.put("/api/sessions/{session_id}/tasks")
    async def upsert_task(session_id: str, body: dict):
        """Upsert a single task card via the io writer.

        The legacy frontend still sends ``content`` (old name for
        description), ``starts_at``/``ends_at`` (dropped in events_v1),
        ``interval``, ``previous_name`` (rename), ``status`` — we accept
        the whitelisted subset and pass the rest to io.upsert_task.
        ``previous_name != name`` → delete + create.
        """
        name = str(body.get("name") or "").strip()
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise HTTPException(400, "Task name is required / invalid")
        previous_name = (body.get("previous_name") or name).strip()

        description = body.get("description")
        if description is None and "content" in body:
            description = body.get("content")
        script = body.get("script")
        notes = body.get("notes") or body.get("comments")
        progress = body.get("progress")

        # Interval normalisation: legacy field was ``interval``.
        raw_interval = body.get("check_interval", body.get("interval"))
        check_interval: float | None
        if raw_interval in (None, ""):
            check_interval = None
        else:
            try:
                check_interval = float(raw_interval)
            except (TypeError, ValueError):
                raise HTTPException(400, "Task interval must be a number of seconds")
            if check_interval < 1:
                raise HTTPException(400, "Task interval must be at least 1 second")

        # Preserve the backward-compat schedule-window validation even
        # though we ignore the values themselves.
        _validate_schedule_window(
            body.get("starts_at") or body.get("start_at"),
            body.get("ends_at") or body.get("end_at"),
        )

        try:
            if previous_name != name:
                # Rename = delete old then create new.
                try:
                    io.delete_task(session_id, previous_name)
                except FileNotFoundError:
                    pass
            io.upsert_task(
                session_id,
                name,
                description=description,
                script=script,
                check_interval=check_interval,
                notes=notes,
                progress=progress,
            )
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"ok": True}

    @app.delete("/api/sessions/{session_id}/tasks/{task_name}")
    async def delete_task(session_id: str, task_name: str):
        normalized = str(task_name or "").strip()
        if not normalized or normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
            raise HTTPException(400, "Task name is invalid")
        try:
            io.delete_task(session_id, normalized)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"ok": True}

    @app.get("/api/sessions/{session_id}/todo_list")
    async def read_todo_list(session_id: str):
        try:
            payload = io.read_todo_list(session_id)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"todo_list": payload}

    @app.post("/api/sessions/{session_id}/todo_list")
    async def set_todo_list(session_id: str, body: dict):
        todo_list = body.get("todo_list")
        if not isinstance(todo_list, dict):
            raise HTTPException(400, "Body must include 'todo_list' object")
        try:
            io.upsert_todo_list(session_id, todo_list)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"ok": True}

    @app.get("/api/sessions/{session_id}/config")
    async def read_config(session_id: str):
        try:
            params = io.read_config(session_id)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"params": params}

    @app.put("/api/sessions/{session_id}/config")
    async def set_config(session_id: str, body: dict):
        params = body.get("params")
        if not isinstance(params, dict):
            raise HTTPException(400, "Body must include a JSON object in 'params'")
        try:
            # io.update_config is single-key; iterate for multi-key body.
            # Unknown keys are silently dropped by the writer's whitelist
            # — matches the service-layer behaviour.
            for key, value in params.items():
                try:
                    io.update_config(session_id, key, value)
                except ValueError:
                    # Unknown key — drop silently (whitelist semantics).
                    continue
            saved = io.read_config(session_id)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"ok": True, "params": saved}

    @app.get("/api/sessions/{session_id}/prompts/{name}")
    async def read_prompt(session_id: str, name: str):
        try:
            text = io.read_prompt(session_id, name)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"text": text}

    @app.put("/api/sessions/{session_id}/prompts/{name}")
    async def update_prompt(session_id: str, name: str, body: dict):
        text = body.get("text")
        if not isinstance(text, str):
            raise HTTPException(400, "Body must include 'text' string")
        try:
            io.update_prompt(session_id, name, text)
            saved = io.read_prompt(session_id, name)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"ok": True, "text": saved}

    @app.get("/api/sessions/{session_id}/assets/{name}")
    async def read_asset(session_id: str, name: str):
        try:
            text = io.read_asset(session_id, name)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"text": text}

    @app.put("/api/sessions/{session_id}/assets/{name}")
    async def update_asset(session_id: str, name: str, body: dict):
        text = body.get("text")
        if not isinstance(text, str):
            raise HTTPException(400, "Body must include 'text' string")
        try:
            io.update_asset(session_id, name, text)
            saved = io.read_asset(session_id, name)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"ok": True, "text": saved}

    # ── Panel ────────────────────────────────────────────────────────────

    @app.get("/api/sessions/{session_id}/panel")
    async def read_panel(session_id: str):
        try:
            return io.read_panel(session_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/sessions/{session_id}/panel/{tid}")
    async def read_panel_entry(session_id: str, tid: str):
        try:
            return io.read_panel_entry(session_id, tid)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/sessions/{session_id}/panel/{tid}/kill")
    async def kill_panel_entry(session_id: str, tid: str):
        try:
            io.kill_panel_entry(session_id, tid)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"ok": True}

    # ── Terminal ─────────────────────────────────────────────────────────

    @app.get("/api/sessions/{session_id}/terminal")
    async def read_terminal(session_id: str):
        try:
            state = io.read_terminal_state(session_id)
            log = io.read_terminal_log(session_id)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"state": state, "log": log}

    @app.get("/api/sessions/{session_id}/terminal/log")
    async def read_terminal_log(session_id: str, offset: int = 0):
        try:
            entries = io.read_terminal_log(session_id, since=offset)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"log": entries}

    @app.post("/api/sessions/{session_id}/terminal/input")
    async def post_terminal_input(session_id: str, body: dict):
        content = body.get("content")
        if not isinstance(content, str):
            raise HTTPException(400, "Body must include 'content' string")
        try:
            io.terminal_input(session_id, content, source="web")
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/terminal/interrupt")
    async def post_terminal_interrupt(session_id: str):
        try:
            io.terminal_interrupt(session_id)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"ok": True}

    # ── Catalogs ─────────────────────────────────────────────────────────

    @app.get("/api/models")
    async def list_models():
        # Preserve the legacy {"providers": [...]} wrapper; io.list_models
        # returns a bare list so we re-wrap here.
        return {"providers": io.list_models()}

    @app.get("/api/agents")
    async def list_agents():
        return {"agents": io.list_agents()}

    # ── WeChat bridge status (retained) ──────────────────────────────────

    @app.get("/api/weixin/status")
    async def weixin_status():
        return {
            "status": weixin.status,
            "error": weixin.error,
            "session": weixin._current_session,
            "account": weixin._account_id,
        }

    return app


# ── Small helpers ────────────────────────────────────────────────────────────

def _validate_schedule_window(start_at, end_at) -> None:
    """Validate the legacy schedule-window fields ``starts_at``/``ends_at``.

    The events_v1 task schema has no schedule window (cadence is on the
    card + script); we still honour the client-side check so a frontend
    pushing an invalid start>end pair gets a 400.
    """
    if not start_at or not end_at:
        return
    try:
        s = datetime.fromisoformat(start_at)
        e = datetime.fromisoformat(end_at)
    except (TypeError, ValueError):
        raise HTTPException(400, "start_at/end_at must be ISO timestamps")
    if e < s:
        raise HTTPException(400, "end_at must be after start_at")


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
