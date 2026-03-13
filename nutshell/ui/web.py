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
import os
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from nutshell.runtime.status import ensure_session_status, read_session_status, write_session_status
from nutshell.runtime.params import ensure_session_params, write_session_params

SESSIONS_DIR = Path("sessions")
_DEFAULT_ENTITY = "entity/agent_core"
_DEFAULT_PORT = 8080


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, PermissionError, ValueError, OSError):
        return False


def _read_session_info(session_dir: Path) -> dict | None:
    """Read session metadata from manifest.json (static) and status.json (dynamic)."""
    manifest_path = session_dir / "_system_log" / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        manifest = {}
    # All dynamic state comes from status.json
    status_payload = read_session_status(session_dir)
    tasks_path = session_dir / "tasks.md"
    has_tasks = tasks_path.exists() and bool(tasks_path.read_text(encoding="utf-8").strip())
    pid_alive = _pid_alive(status_payload.get("pid"))
    status = status_payload.get("status", "active")
    return {
        "id": session_dir.name,
        "entity": manifest.get("entity", "?"),
        "created_at": manifest.get("created_at", ""),
        "heartbeat": manifest.get("heartbeat", 10.0),
        "pid_alive": pid_alive,
        "status": status,
        "has_tasks": has_tasks,
        "model_state": status_payload.get("model_state", "idle"),
        "model_source": status_payload.get("model_source"),
        "last_run_at": status_payload.get("last_run_at"),
        "tasks_updated_at": status_payload.get("tasks_updated_at"),
        "heartbeat_interval": status_payload.get("heartbeat_interval", 600.0),
        "alive": pid_alive and status != "stopped",
    }


def _session_priority(info: dict) -> int:
    """Return sort priority: 0=running, 1=napping(tasks queued), 2=stopped, 3=idle."""
    if info.get("model_state") == "running" and info.get("pid_alive") and info.get("status") != "stopped":
        return 0
    if info.get("has_tasks") and info.get("pid_alive") and info.get("status") != "stopped":
        return 1
    if info.get("status") == "stopped":
        return 2
    return 3


def _sort_sessions(sessions: list[dict]) -> list[dict]:
    """Sort sessions: running > queued > idle > stopped, then by most recently run, then by creation time."""
    # Step 1: stable sort by timestamp descending (most recent first)
    sessions.sort(key=lambda s: s.get("last_run_at") or s.get("created_at") or "", reverse=True)
    # Step 2: stable sort by priority ascending — preserves timestamp order within groups
    sessions.sort(key=_session_priority)
    return sessions


# ── FastAPI app ────────────────────────────────────────────────────────────

def create_app(sessions_dir: Path) -> FastAPI:
    app = FastAPI(title="Nutshell Web UI", docs_url=None, redoc_url=None)

    # ── HTML ──────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return _HTML

    # ── Sessions ──────────────────────────────────────────────────────────

    @app.get("/api/sessions")
    async def list_sessions():
        if not sessions_dir.exists():
            return []
        result = []
        for d in sessions_dir.iterdir():
            if not d.is_dir():
                continue
            info = _read_session_info(d)
            if info is not None:
                result.append(info)
        return _sort_sessions(result)

    @app.post("/api/sessions")
    async def create_session(body: dict):
        session_id = body.get("id") or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        entity = body.get("entity", _DEFAULT_ENTITY)
        heartbeat = float(body.get("heartbeat", 600.0))

        session_dir = sessions_dir / session_id
        system_log = session_dir / "_system_log"
        session_dir.mkdir(parents=True, exist_ok=True)
        system_log.mkdir(exist_ok=True)
        (session_dir / "files").mkdir(exist_ok=True)
        (session_dir / "prompts").mkdir(exist_ok=True)
        (session_dir / "skills").mkdir(exist_ok=True)
        (session_dir / "tools").mkdir(exist_ok=True)
        (system_log / "context.jsonl").touch(exist_ok=True)
        (system_log / "events.jsonl").touch(exist_ok=True)
        (session_dir / "tasks.md").touch(exist_ok=True)
        memory_path = session_dir / "prompts" / "memory.md"
        if not memory_path.exists():
            memory_path.write_text("", encoding="utf-8")

        # manifest.json is purely static config — written once, never mutated
        manifest = {
            "session_id": session_id,
            "entity": entity,
            "created_at": datetime.now().isoformat(),
        }
        (system_log / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        # params.json is the source of truth for heartbeat_interval, model, provider
        ensure_session_params(session_dir, heartbeat_interval=heartbeat)
        write_session_params(session_dir, heartbeat_interval=heartbeat)
        # status.json mirrors heartbeat_interval for UI read access
        ensure_session_status(session_dir)
        write_session_status(session_dir, heartbeat_interval=heartbeat)
        return {"id": session_id, "entity": entity}

    # ── Messages ──────────────────────────────────────────────────────────

    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, body: dict):
        from nutshell.runtime.ipc import FileIPC
        session_dir = sessions_dir / session_id
        if not session_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")
        ipc = FileIPC(session_dir)
        msg_id = ipc.send_message(body.get("content", ""))
        return {"id": msg_id}

    # ── SSE events ────────────────────────────────────────────────────────

    @app.get("/api/sessions/{session_id}/events")
    async def stream_events(session_id: str, context_since: int = 0, events_since: int = 0):
        session_dir = sessions_dir / session_id
        if not session_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")

        async def generator() -> AsyncIterator[str]:
            from nutshell.runtime.ipc import FileIPC
            ipc = FileIPC(session_dir)
            ctx_offset = context_since
            evt_offset = events_since
            # Drain events that appeared between history load and SSE connect
            for event, new_offset in ipc.tail_context(ctx_offset):
                ctx_offset = new_offset
                yield _sse_format(event)
            for event, new_offset in ipc.tail_runtime_events(evt_offset):
                evt_offset = new_offset
                yield _sse_format(event)
            # Stream new events as they arrive.
            # Context-first per poll cycle: model_status(idle) is written to events.jsonl
            # AFTER the turn is written to context.jsonl, so reading context first ensures
            # the agent message renders before the thinking bubble is cleared.
            while True:
                await asyncio.sleep(0.3)
                for event, new_offset in ipc.tail_context(ctx_offset):
                    ctx_offset = new_offset
                    yield _sse_format(event)
                for event, new_offset in ipc.tail_runtime_events(evt_offset):
                    evt_offset = new_offset
                    yield _sse_format(event)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ── History ───────────────────────────────────────────────────────────

    @app.get("/api/sessions/{session_id}/history")
    async def get_history(session_id: str):
        """Return all display events from context.jsonl + both file offsets.

        JS loads this once on attach to render full history instantly, then
        starts SSE from the returned offsets for new events only.
        Only context.jsonl is read (pure conversation history); events.jsonl
        ephemeral streaming events are not replayed.
        """
        from nutshell.runtime.ipc import FileIPC
        ipc = FileIPC(sessions_dir / session_id)
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

    # ── Stop / Start ──────────────────────────────────────────────────────

    @app.post("/api/sessions/{session_id}/stop")
    async def stop_session(session_id: str):
        from nutshell.runtime.ipc import FileIPC
        session_dir = sessions_dir / session_id
        write_session_status(session_dir, status="stopped", stopped_at=datetime.now().isoformat())
        if session_dir.exists():
            FileIPC(session_dir).append_event(
                {"type": "status", "value": "heartbeat paused — use ▶ Start to resume"}
            )
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/start")
    async def start_session(session_id: str):
        from nutshell.runtime.ipc import FileIPC
        session_dir = sessions_dir / session_id
        write_session_status(session_dir, status="active", stopped_at=None)
        if session_dir.exists():
            FileIPC(session_dir).append_event(
                {"type": "status", "value": "heartbeat resumed"}
            )
        return {"ok": True}

    # ── Tasks ─────────────────────────────────────────────────────────────

    @app.get("/api/sessions/{session_id}/tasks")
    async def get_tasks(session_id: str):
        tasks_path = sessions_dir / session_id / "tasks.md"
        if not tasks_path.exists():
            return {"content": ""}
        return {"content": tasks_path.read_text(encoding="utf-8")}

    @app.put("/api/sessions/{session_id}/tasks")
    async def set_tasks(session_id: str, body: dict):
        session_dir = sessions_dir / session_id
        if not session_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")
        tasks_path = session_dir / "tasks.md"
        tasks_path.write_text(body.get("content", ""), encoding="utf-8")
        write_session_status(session_dir, tasks_updated_at=datetime.now().isoformat())
        return {"ok": True}

    return app


def _sse_format(event: dict) -> str:
    etype = event.get("type", "message")
    data = json.dumps(event, ensure_ascii=False)
    return f"event: {etype}\ndata: {data}\n\n"


# ── Embedded HTML ──────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Nutshell</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg: #0d1117; --bg2: #161b22; --bg3: #21262d;
    --border: #30363d; --accent: #58a6ff; --green: #3fb950;
    --yellow: #d29922; --red: #f85149; --text: #c9d1d9; --muted: #8b949e;
  }

  body { background: var(--bg); color: var(--text); font-family: 'SF Mono', 'Fira Code', monospace; font-size: 13px; height: 100vh; display: flex; flex-direction: column; }

  /* Header */
  #header { background: var(--bg2); border-bottom: 1px solid var(--border); padding: 10px 16px; display: flex; align-items: center; gap: 12px; }
  #header h1 { font-size: 15px; font-weight: 600; color: var(--accent); }
  #server-indicator { display: flex; align-items: center; gap: 8px; padding: 4px 10px; border: 1px solid var(--border); border-radius: 999px; font-size: 11px; color: var(--muted); }
  #server-indicator.on { border-color: rgba(63, 185, 80, 0.45); color: #b8f3c0; background: rgba(63, 185, 80, 0.12); }
  #server-indicator.off { border-color: rgba(248, 81, 73, 0.45); color: #ffb7b1; background: rgba(248, 81, 73, 0.12); }
  #server-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  #server-indicator.on #server-dot { background: var(--green); }
  #server-indicator.off #server-dot { background: var(--red); }
  #header-meta { margin-left: auto; display: flex; align-items: center; gap: 10px; }
  #session-name { font-size: 11px; color: var(--muted); }
  #session-indicator { display: flex; align-items: center; gap: 8px; padding: 4px 10px; border: 1px solid var(--border); border-radius: 999px; font-size: 11px; color: var(--muted); }
  #session-indicator.running { border-color: rgba(63, 185, 80, 0.45); color: #b8f3c0; background: rgba(63, 185, 80, 0.12); }
  #session-indicator.napping { border-color: rgba(210, 153, 34, 0.45); color: #f4d48c; background: rgba(210, 153, 34, 0.12); }
  #session-indicator.stopped { border-color: rgba(248, 81, 73, 0.45); color: #ffb7b1; background: rgba(248, 81, 73, 0.12); }
  #session-indicator.idle { border-color: var(--border); color: var(--muted); background: transparent; }
  #session-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  #session-indicator.running #session-dot { background: var(--green); animation: pulse-dot 1.5s ease-in-out infinite; }
  #session-indicator.napping #session-dot { background: var(--yellow); animation: pulse-dot 2.5s ease-in-out infinite; }
  #session-indicator.stopped #session-dot { background: var(--red); }
  #session-indicator.idle #session-dot { background: var(--muted); }

  @keyframes pulse-dot { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }

  /* Layout */
  #main { display: flex; flex: 1; overflow: hidden; }
  #sidebar { width: 220px; background: var(--bg2); border-right: 1px solid var(--border); display: flex; flex-direction: column; }
  #chat-area { flex: 1; display: flex; flex-direction: column; }
  #tasks-panel { width: 240px; background: var(--bg2); border-left: 1px solid var(--border); display: flex; flex-direction: column; }

  /* Sidebar */
  .panel-header { padding: 10px 12px; font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
  #session-list { flex: 1; overflow-y: auto; }
  .session-item { padding: 8px 12px; cursor: pointer; border-bottom: 1px solid var(--border); transition: background 0.1s; }
  .session-item:hover { background: var(--bg3); }
  .session-item.active { background: var(--bg3); border-left: 2px solid var(--accent); }
  .session-name { font-weight: 500; color: var(--text); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .session-meta { font-size: 10px; color: var(--muted); margin-top: 2px; }
  .dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }
  .dot.running { background: var(--green); animation: pulse-dot 1.5s ease-in-out infinite; }
  .dot.napping { background: var(--yellow); animation: pulse-dot 2.5s ease-in-out infinite; }
  .dot.stopped { background: var(--red); }
  .dot.idle { background: var(--muted); }
  #new-btn { margin: 10px 10px 0; padding: 6px 10px; background: var(--accent); color: #000; border: none; border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 600; }
  #new-btn:hover { opacity: 0.85; }
  .session-controls { display: flex; gap: 4px; margin: 4px 10px 10px; }
  .ctrl-btn { flex: 1; padding: 4px 0; border: 1px solid var(--border); border-radius: 4px; cursor: pointer; font-size: 11px; background: var(--bg3); color: var(--muted); }
  .ctrl-btn:hover { color: var(--text); border-color: var(--muted); }
  .ctrl-btn.stop:hover { color: var(--red); border-color: var(--red); }
  .ctrl-btn.start:hover { color: var(--green); border-color: var(--green); }

  /* Chat */
  #messages { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
  .msg { padding: 6px 10px; border-radius: 6px; max-width: 90%; line-height: 1.6; word-break: break-word; }
  .msg.agent { background: var(--bg3); border-left: 3px solid var(--accent); color: var(--text); }
  .msg.agent.heartbeat-agent { border-left-color: #6b9fd4; opacity: 0.85; }
  .msg.user  { background: #1c2a3a; border-left: 3px solid var(--green); color: var(--text); align-self: flex-end; }
  .msg.tool  { background: var(--bg2); color: var(--yellow); font-size: 11px; border-left: 3px solid var(--yellow); white-space: pre-wrap; }
  .msg.heartbeat_trigger { background: #1a2030; border-left: 3px solid var(--accent); color: var(--text); align-self: flex-end; }
  .msg.heartbeat_finished { color: var(--muted); font-size: 11px; }
  .msg.status { color: var(--muted); font-size: 11px; text-align: center; align-self: center; }
  .msg.error  { background: #2d1515; border-left: 3px solid var(--red); color: var(--red); }
  .msg-label  { font-size: 10px; color: var(--muted); margin-bottom: 4px; }
  .msg-ts { font-size: 10px; color: var(--muted); opacity: 0.5; margin-top: 4px; }
  .msg-inline { display: inline-flex; align-items: center; gap: 8px; }
  .msg-inline-ts { font-size: 10px; color: var(--muted); opacity: 0.75; }

  /* Thinking bubble */
  .msg.thinking { background: var(--bg3); border-left: 3px solid var(--accent); color: var(--muted); }
  .thinking-dots { display: inline-flex; gap: 4px; align-items: center; height: 20px; }
  .thinking-dots span { width: 6px; height: 6px; background: var(--accent); border-radius: 50%; display: inline-block; animation: thinking-bounce 1.2s ease-in-out infinite; }
  .thinking-dots span:nth-child(2) { animation-delay: 0.2s; }
  .thinking-dots span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes thinking-bounce { 0%,60%,100% { transform: translateY(0); opacity: 0.5; } 30% { transform: translateY(-5px); opacity: 1; } }

  /* Markdown rendering inside agent messages */
  .msg.agent p { margin: 0.4em 0; }
  .msg.agent p:first-child { margin-top: 0; }
  .msg.agent p:last-child { margin-bottom: 0; }
  .msg.agent code { background: var(--bg); padding: 1px 5px; border-radius: 3px; font-family: inherit; font-size: 12px; }
  .msg.agent pre { background: var(--bg); padding: 8px 10px; border-radius: 4px; overflow-x: auto; margin: 6px 0; }
  .msg.agent pre code { padding: 0; background: none; }
  .msg.agent ul, .msg.agent ol { margin: 0.4em 0 0.4em 1.4em; }
  .msg.agent li { margin: 0.2em 0; }
  .msg.agent h1, .msg.agent h2, .msg.agent h3, .msg.agent h4 { margin: 0.6em 0 0.3em; font-weight: 600; }
  .msg.agent blockquote { border-left: 3px solid var(--border); padding-left: 8px; color: var(--muted); margin: 0.4em 0; }
  .msg.agent table { border-collapse: collapse; width: 100%; margin: 0.4em 0; }
  .msg.agent th, .msg.agent td { border: 1px solid var(--border); padding: 4px 8px; }
  .msg.agent th { background: var(--bg2); }
  .msg.agent a { color: var(--accent); }
  .msg.user p { margin: 0; }

  /* Input */
  #input-row { padding: 10px 12px; border-top: 1px solid var(--border); display: flex; gap: 8px; background: var(--bg2); }
  #msg-input { flex: 1; background: var(--bg3); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; font-family: inherit; font-size: 13px; outline: none; }
  #msg-input:focus { border-color: var(--accent); }
  #send-btn { padding: 8px 14px; background: var(--accent); color: #000; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 13px; }
  #send-btn:hover { opacity: 0.85; }

  /* Tasks */
  #tasks-content { flex: 1; padding: 10px 12px; overflow-y: auto; white-space: pre-wrap; font-size: 12px; color: var(--text); line-height: 1.6; }
  #tasks-edit { display: none; flex-direction: column; flex: 1; padding: 8px; gap: 6px; }
  #tasks-textarea { flex: 1; background: var(--bg3); color: var(--text); border: 1px solid var(--border); border-radius: 4px; padding: 6px; font-family: inherit; font-size: 12px; resize: none; outline: none; }
  #tasks-save { padding: 4px 10px; background: var(--green); color: #000; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; }
  #tasks-cancel { padding: 4px 10px; background: var(--bg3); color: var(--muted); border: 1px solid var(--border); border-radius: 4px; cursor: pointer; font-size: 12px; }
  .tasks-actions { display: flex; gap: 6px; }
  #tasks-edit-btn { cursor: pointer; font-size: 11px; color: var(--muted); border: none; background: none; }
  #tasks-edit-btn:hover { color: var(--accent); }
</style>
</head>
<body>

<div id="header">
  <h1>🥜 nutshell</h1>
  <div id="server-indicator" class="off">
    <span id="server-dot"></span>
    <span id="server-state">server off</span>
  </div>
  <div id="header-meta">
    <span id="session-name">no session selected</span>
    <div id="session-indicator" class="idle">
      <span id="session-dot"></span>
      <span id="session-state">idle</span>
    </div>
  </div>
</div>

<div id="main">
  <!-- Sidebar: sessions -->
  <div id="sidebar">
    <div class="panel-header">
      Sessions
      <button id="new-btn" onclick="showNewSessionDialog()">+ New</button>
    </div>
    <div id="session-list"></div>
    <div class="session-controls">
      <button class="ctrl-btn stop" onclick="stopSession()">⏸ Stop</button>
      <button class="ctrl-btn start" onclick="startSession()">▶ Start</button>
    </div>
  </div>

  <!-- Chat -->
  <div id="chat-area">
    <div id="messages">
      <div class="msg status">Select or create a session to start chatting.</div>
    </div>
    <div id="input-row">
      <input id="msg-input" type="text" placeholder="Type a message..." disabled onkeydown="onInputKey(event)">
      <button id="send-btn" onclick="sendMessage()" disabled>Send</button>
    </div>
  </div>

  <!-- Tasks -->
  <div id="tasks-panel">
    <div class="panel-header">
      Tasks
      <span id="tasks-updated-ts" style="font-size:10px; color: var(--muted); margin-right: auto; margin-left: 6px;"></span>
      <button id="tasks-edit-btn" onclick="toggleTasksEdit()">edit</button>
    </div>
    <div id="tasks-content">(no session selected)</div>
    <div id="tasks-edit">
      <textarea id="tasks-textarea" placeholder="Add tasks here..."></textarea>
      <div class="tasks-actions">
        <button id="tasks-save" onclick="saveTasks()">Save</button>
        <button id="tasks-cancel" onclick="toggleTasksEdit()">Cancel</button>
      </div>
    </div>
  </div>
</div>

<script>
  let currentSession = null;
  let eventSource = null;
  let sessions = [];
  let modelState = { state: 'idle', source: null };
  let thinkingEl = null;  // The active "thinking" bubble element

  // Configure marked for safe rendering
  if (typeof marked !== 'undefined') {
    marked.setOptions({ breaks: true, gfm: true });
  }

  // ── Init ──────────────────────────────────────────────────────────────

  async function init() {
    await refreshSessions();
    setInterval(refreshSessions, 3000);
    setInterval(refreshTasks, 2000);
  }

  // ── Sessions ──────────────────────────────────────────────────────────

  async function refreshSessions() {
    try {
      const res = await fetch('/api/sessions');
      sessions = await res.json();
    } catch (e) { return; }
    syncModelStateFromMeta();
    renderServerIndicator();
    renderSessionList();
    renderSessionIndicator();
  }

  function renderServerIndicator() {
    const hasRunningDaemon = sessions.some(sess => sess.pid_alive);
    const el = document.getElementById('server-indicator');
    const state = document.getElementById('server-state');
    el.className = hasRunningDaemon ? 'on' : 'off';
    state.textContent = hasRunningDaemon ? 'server on' : 'server off';
  }

  function renderSessionList() {
    const list = document.getElementById('session-list');
    list.innerHTML = '';
    // Sessions arrive pre-sorted from the API
    for (const sess of sessions) {
      const tone = sessionTone(sess);
      const div = document.createElement('div');
      div.className = 'session-item' + (sess.id === currentSession ? ' active' : '');
      div.onclick = () => attachSession(sess.id);
      const lastRun = sess.last_run_at ? fmtDate(sess.last_run_at) : (sess.created_at ? fmtDate(sess.created_at) : '');
      div.innerHTML = `
        <div class="session-name">
          <span class="dot ${tone}"></span>${escHtml(sess.id)}
        </div>
        <div class="session-meta">${escHtml(sess.entity)}${lastRun ? ' · ' + lastRun : ''}</div>
      `;
      list.appendChild(div);
    }
  }

  async function attachSession(id) {
    if (id === currentSession) return;
    currentSession = id;
    renderSessionList();

    // Close old SSE
    if (eventSource) { eventSource.close(); eventSource = null; }
    hideThinking();

    // Clear chat
    const msgs = document.getElementById('messages');
    msgs.innerHTML = '';
    modelState = { state: 'idle', source: null };
    renderServerIndicator();
    renderSessionIndicator();

    // Enable input
    document.getElementById('msg-input').disabled = false;
    document.getElementById('send-btn').disabled = false;

    // Load full history from context.jsonl (pure conversation: user + tool + agent events)
    const histRes = await fetch(`/api/sessions/${id}/history`);
    const { events, context_offset, events_offset } = await histRes.json();
    for (const event of events) appendEvent(event);
    syncModelStateFromMeta();
    renderSessionIndicator();
    // Restore thinking bubble if model is currently running
    if (modelState.state === 'running') showThinking();

    // SSE from both offsets — context.jsonl for new turns, events.jsonl for runtime events
    eventSource = new EventSource(
      `/api/sessions/${id}/events?context_since=${context_offset}&events_since=${events_offset}`
    );
    const types = ['agent','user','tool','model_status','partial_text','heartbeat_trigger','heartbeat_finished','status','error'];
    for (const t of types) {
      eventSource.addEventListener(t, e => appendEvent(JSON.parse(e.data)));
    }

    refreshTasks();
  }

  async function showNewSessionDialog() {
    const id = prompt('Session ID (leave empty for timestamp):') ?? null;
    if (id === null) return;
    const entity = prompt('Entity path:', 'entity/agent_core');
    if (!entity) return;
    const res = await fetch('/api/sessions', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ id: id || '', entity }),
    });
    const data = await res.json();
    await refreshSessions();
    attachSession(data.id);
  }

  // ── Thinking bubble ───────────────────────────────────────────────────

  function showThinking(text) {
    const msgs = document.getElementById('messages');
    if (!thinkingEl) {
      thinkingEl = document.createElement('div');
      thinkingEl.className = 'msg thinking';
      thinkingEl.id = 'thinking-bubble';
      msgs.appendChild(thinkingEl);
    }
    if (text) {
      const rendered = typeof marked !== 'undefined' ? marked.parse(text) : escHtml(text);
      thinkingEl.innerHTML = '<div class="msg-label">agent</div><div class="thinking-text">' + rendered + '</div>';
    } else {
      thinkingEl.innerHTML = '<div class="msg-label">agent</div><div class="thinking-dots"><span></span><span></span><span></span></div>';
    }
    msgs.scrollTop = msgs.scrollHeight;
  }

  function hideThinking() {
    if (thinkingEl) {
      thinkingEl.remove();
      thinkingEl = null;
    }
  }

  // ── Chat ──────────────────────────────────────────────────────────────

  function fmtTime(ts) {
    if (!ts) return '';
    try {
      const d = new Date(ts);
      return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch { return ''; }
  }

  function fmtDate(ts) {
    if (!ts) return '';
    try {
      const d = new Date(ts);
      const now = new Date();
      const isToday = d.toDateString() === now.toDateString();
      if (isToday) return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
      return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    } catch { return ''; }
  }

  function renderMarkdown(text) {
    if (typeof marked !== 'undefined') {
      return marked.parse(text || '');
    }
    return '<pre style="white-space:pre-wrap">' + escHtml(text || '') + '</pre>';
  }

  function appendEvent(event) {
    const msgs = document.getElementById('messages');
    const etype = event.type || 'message';

    if (etype === 'model_status') {
      modelState = { state: event.state || 'idle', source: event.source || null };
      const meta = currentSessionMeta();
      if (meta) {
        meta.model_state = modelState.state;
        meta.model_source = modelState.source;
      }
      if (modelState.state === 'running') {
        if (!thinkingEl) showThinking();
      } else {
        // Model is now idle. The turn/heartbeat_finished event was written BEFORE
        // model_status(idle), so any agent event already arrived and called hideThinking().
        // This call handles edge cases (error, cancelled, heartbeat SESSION_FINISHED).
        hideThinking();
      }
      renderSessionIndicator();
      renderSessionList();
      return;
    }

    if (etype === 'partial_text') {
      // Update the thinking bubble with streamed text
      showThinking(event.content || '');
      return;
    }

    const div = document.createElement('div');
    div.className = `msg ${etype}`;

    let label = '';
    let html = '';

    if (etype === 'agent') {
      hideThinking();  // Replace thinking bubble with actual response
      label = event.triggered_by === 'heartbeat' ? '⏱ agent' : 'agent';
      div.className += event.triggered_by === 'heartbeat' ? ' heartbeat-agent' : '';
      html = renderMarkdown(event.content || '');
    } else if (etype === 'user') {
      label = 'you';
      html = '<p>' + escHtml(event.content || '') + '</p>';
    } else if (etype === 'tool') {
      html = escHtml(`[${event.name}] ` + JSON.stringify(event.input || {}));
    } else if (etype === 'heartbeat_trigger') {
      label = '⏱ server';
      html = '<em>heartbeat — checking tasks</em>';
    } else if (etype === 'heartbeat_finished') {
      hideThinking();
      html = '[session finished — all tasks done]';
    } else if (etype === 'status') {
      html = event.value === 'cancelled' || event.value === 'stopped'
        ? '[server stopped]'
        : `[status: ${escHtml(String(event.value || ''))}]`;
    } else if (etype === 'error') {
      hideThinking();
      html = `[error] ${escHtml(event.content || '')}`;
    } else {
      html = escHtml(JSON.stringify(event));
    }

    const ts = fmtTime(event.ts);
    const tsHtml = ts ? `<div class="msg-ts">${ts}</div>` : '';

    if (etype === 'status') {
      const prefix = ts ? `<span class="msg-inline-ts">${ts}</span> ` : '';
      div.innerHTML = `<div class="msg-inline">${prefix}${html}</div>`;
    } else if (label) {
      div.innerHTML = `<div class="msg-label">${label}</div>${html}${tsHtml}`;
    } else {
      div.innerHTML = `${html}${tsHtml}`;
    }

    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
  }

  function currentSessionMeta() {
    return sessions.find(sess => sess.id === currentSession) || null;
  }

  function syncModelStateFromMeta() {
    const meta = currentSessionMeta();
    modelState = {
      state: meta?.model_state || 'idle',
      source: meta?.model_source || null,
    };
  }

  function sessionTone(session) {
    if (!session) return 'idle';
    if (session.pid_alive && session.model_state === 'running' && session.status !== 'stopped') return 'running';
    if (session.has_tasks && session.pid_alive && session.status !== 'stopped') return 'napping';
    if (session.status === 'stopped') return 'stopped';
    return 'idle';
  }

  function renderSessionIndicator() {
    const meta = currentSessionMeta();
    const nameEl = document.getElementById('session-name');
    const indicator = document.getElementById('session-indicator');
    const stateEl = document.getElementById('session-state');
    if (!meta) {
      nameEl.textContent = 'no session selected';
      indicator.className = 'idle';
      stateEl.textContent = 'idle';
      return;
    }

    nameEl.textContent = meta.id;

    let tone = 'idle';
    let label = 'idle';
    if (meta.pid_alive && (meta.model_state === 'running' || modelState.state === 'running') && meta.status !== 'stopped') {
      tone = 'running';
      label = `running (${modelState.source || meta.model_source || 'user'})`;
    } else if (meta.has_tasks && meta.pid_alive && meta.status !== 'stopped') {
      tone = 'napping';
      label = 'napping';
    } else if (meta.status === 'stopped') {
      tone = 'stopped';
      label = 'stopped';
    }

    indicator.className = tone;
    stateEl.textContent = label;
  }

  function onInputKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  }

  async function sendMessage() {
    const input = document.getElementById('msg-input');
    const text = input.value.trim();
    if (!text || !currentSession) return;
    input.value = '';

    // Optimistic UI update: if session is stopped, immediately show as running
    // (daemon will pick up the message within 0.5s and auto-resume)
    const meta = currentSessionMeta();
    if (meta && meta.status === 'stopped') {
      meta.status = 'active';
      meta.model_state = 'running';
      modelState = { state: 'running', source: 'user' };
      renderSessionIndicator();
      renderSessionList();
      if (!thinkingEl) showThinking();
    }

    await fetch(`/api/sessions/${currentSession}/messages`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ content: text }),
    });
  }

  // ── Tasks ─────────────────────────────────────────────────────────────

  async function refreshTasks() {
    if (!currentSession) return;
    try {
      const res = await fetch(`/api/sessions/${currentSession}/tasks`);
      const data = await res.json();
      const content = data.content?.trim() || '(empty)';
      document.getElementById('tasks-content').textContent = content;
      document.getElementById('tasks-textarea').value = data.content || '';
      const meta = currentSessionMeta();
      if (meta) meta.has_tasks = Boolean(data.content?.trim());
      // Show tasks last update time
      const tsEl = document.getElementById('tasks-updated-ts');
      const updatedAt = meta?.tasks_updated_at;
      const interval = meta?.heartbeat_interval;
      const intervalStr = interval
        ? (interval >= 60 ? `every ${Math.round(interval / 60)}m` : `every ${interval}s`)
        : '';
      const updatedStr = updatedAt ? fmtDate(updatedAt) : '';
      tsEl.textContent = [updatedStr, intervalStr].filter(Boolean).join(' · ');
      renderServerIndicator();
      renderSessionIndicator();
      renderSessionList();
    } catch (e) {}
  }

  function toggleTasksEdit() {
    const view = document.getElementById('tasks-content');
    const edit = document.getElementById('tasks-edit');
    const isEditing = edit.style.display === 'flex';
    view.style.display = isEditing ? 'block' : 'none';
    edit.style.display = isEditing ? 'none' : 'flex';
  }

  async function saveTasks() {
    if (!currentSession) return;
    const content = document.getElementById('tasks-textarea').value;
    await fetch(`/api/sessions/${currentSession}/tasks`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ content }),
    });
    toggleTasksEdit();
    refreshTasks();
  }

  // ── Stop / Start ──────────────────────────────────────────────────────

  async function stopSession() {
    if (!currentSession) return;
    await fetch(`/api/sessions/${currentSession}/stop`, { method: 'POST' });
    await refreshSessions();
  }

  async function startSession() {
    if (!currentSession) return;
    await fetch(`/api/sessions/${currentSession}/start`, { method: 'POST' });
    await refreshSessions();
  }

  // ── Utils ─────────────────────────────────────────────────────────────

  function escHtml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  init();
</script>
</body>
</html>
"""


# ── Entry point ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Nutshell Web UI")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--sessions-dir", default=str(SESSIONS_DIR), metavar="DIR")
    args = parser.parse_args()

    sessions_dir = Path(args.sessions_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)

    app = create_app(sessions_dir)
    print(f"nutshell web UI: http://localhost:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
