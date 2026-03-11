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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from nutshell.runtime.status import ensure_session_status, read_session_status

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
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        manifest = {}
    tasks_path = session_dir / "tasks.md"
    has_tasks = tasks_path.exists() and bool(tasks_path.read_text(encoding="utf-8").strip())
    pid_alive = _pid_alive(manifest.get("pid"))
    status = manifest.get("status", "active")
    status_payload = read_session_status(session_dir)
    return {
        "id": session_dir.name,
        "entity": manifest.get("entity", "?"),
        "created_at": manifest.get("created_at", ""),
        "pid_alive": pid_alive,
        "status": status,
        "has_tasks": has_tasks,
        "model_state": status_payload.get("model_state", "idle"),
        "model_source": status_payload.get("model_source"),
        "alive": pid_alive and status != "stopped",
    }


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
        for d in sorted(sessions_dir.iterdir()):
            if not d.is_dir():
                continue
            info = _read_session_info(d)
            if info is not None:
                result.append(info)
        return result

    @app.post("/api/sessions")
    async def create_session(body: dict):
        session_id = body.get("id") or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        entity = body.get("entity", _DEFAULT_ENTITY)
        heartbeat = float(body.get("heartbeat", 10.0))

        session_dir = sessions_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "files").mkdir(exist_ok=True)
        (session_dir / "context.jsonl").touch(exist_ok=True)
        (session_dir / "tasks.md").touch(exist_ok=True)

        manifest = {
            "session_id": session_id,
            "entity": entity,
            "created_at": datetime.now().isoformat(),
            "heartbeat": heartbeat,
        }
        (session_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        ensure_session_status(session_dir)
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
    async def stream_events(session_id: str, since: int = 0):
        session_dir = sessions_dir / session_id
        if not session_dir.exists():
            raise HTTPException(404, f"Session not found: {session_id}")

        async def generator() -> AsyncIterator[str]:
            from nutshell.runtime.ipc import FileIPC
            ipc = FileIPC(session_dir)
            offset = since
            # Yield existing events first
            for event, new_offset in ipc.tail_display(offset):
                offset = new_offset
                yield _sse_format(event)
            # Then stream new events
            while True:
                await asyncio.sleep(0.3)
                for event, new_offset in ipc.tail_display(offset):
                    offset = new_offset
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
        """Return all display events derived from context.jsonl + current byte offset.

        JS loads this once on attach to render full history instantly,
        then starts SSE from the returned offset for new events only.
        """
        from nutshell.runtime.ipc import FileIPC
        ipc = FileIPC(sessions_dir / session_id)
        events: list[dict] = []
        size = 0
        for event, offset in ipc.tail_display(0):
            events.append(event)
            size = offset
        return {"events": events, "offset": size}

    # ── Stop / Start ──────────────────────────────────────────────────────

    @app.post("/api/sessions/{session_id}/stop")
    async def stop_session(session_id: str):
        from nutshell.runtime.ipc import FileIPC
        session_dir = sessions_dir / session_id
        _set_manifest_status(session_dir / "manifest.json", "stopped")
        if session_dir.exists():
            FileIPC(session_dir).append(
                {"type": "status", "value": "heartbeat paused — use ▶ Start to resume"}
            )
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/start")
    async def start_session(session_id: str):
        from nutshell.runtime.ipc import FileIPC
        session_dir = sessions_dir / session_id
        _set_manifest_status(session_dir / "manifest.json", "active")
        if session_dir.exists():
            FileIPC(session_dir).append(
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
        return {"ok": True}

    return app


def _set_manifest_status(manifest_path: Path, status: str) -> None:
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = status
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _sse_format(event: dict) -> str:
    etype = event.get("type", "message")
    data = json.dumps(event, ensure_ascii=False)
    return f"event: {etype}\ndata: {data}\n\n"


# ── Embedded HTML ──────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Nutshell</title>
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
  #session-indicator.queued { border-color: rgba(210, 153, 34, 0.45); color: #f4d48c; background: rgba(210, 153, 34, 0.12); }
  #session-indicator.idle { border-color: var(--border); color: var(--muted); background: transparent; }
  #session-indicator.stopped { border-color: rgba(248, 81, 73, 0.45); color: #ffb7b1; background: rgba(248, 81, 73, 0.12); }
  #session-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  #session-indicator.running #session-dot { background: var(--green); }
  #session-indicator.queued #session-dot { background: var(--yellow); }
  #session-indicator.idle #session-dot { background: var(--muted); }
  #session-indicator.stopped #session-dot { background: var(--red); }

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
  .dot.running { background: var(--green); }
  .dot.queued { background: var(--yellow); }
  .dot.idle { background: var(--muted); }
  .dot.stopped { background: var(--red); }
  #new-btn { margin: 10px 10px 0; padding: 6px 10px; background: var(--accent); color: #000; border: none; border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 600; }
  #new-btn:hover { opacity: 0.85; }
  .session-controls { display: flex; gap: 4px; margin: 4px 10px 10px; }
  .ctrl-btn { flex: 1; padding: 4px 0; border: 1px solid var(--border); border-radius: 4px; cursor: pointer; font-size: 11px; background: var(--bg3); color: var(--muted); }
  .ctrl-btn:hover { color: var(--text); border-color: var(--muted); }
  .ctrl-btn.stop:hover { color: var(--red); border-color: var(--red); }
  .ctrl-btn.start:hover { color: var(--green); border-color: var(--green); }

  /* Chat */
  #messages { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
  .msg { padding: 6px 10px; border-radius: 6px; max-width: 90%; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
  .msg.agent { background: var(--bg3); border-left: 3px solid var(--accent); color: var(--text); }
  .msg.agent.heartbeat-agent { border-left-color: #6b9fd4; opacity: 0.85; }
  .msg.user  { background: #1c2a3a; border-left: 3px solid var(--green); color: var(--text); align-self: flex-end; }
  .msg.tool  { background: var(--bg2); color: var(--yellow); font-size: 11px; border-left: 3px solid var(--yellow); }
  .msg.heartbeat_trigger { background: #1a2535; border: 1px dashed #3a5a8a; color: #6b9fd4; font-size: 11px; align-self: flex-end; border-radius: 12px; padding: 3px 10px; }
  .msg.heartbeat_finished { color: var(--muted); font-size: 11px; }
  .msg.status { color: var(--muted); font-size: 11px; text-align: center; align-self: center; }
  .msg.error  { background: #2d1515; border-left: 3px solid var(--red); color: var(--red); }
  .msg-label  { font-size: 10px; color: var(--muted); margin-bottom: 2px; }
  .msg-ts { font-size: 10px; color: var(--muted); opacity: 0.5; margin-top: 3px; }
  .msg-inline { display: inline-flex; align-items: center; gap: 8px; }
  .msg-inline-ts { font-size: 10px; color: var(--muted); opacity: 0.75; }

  /* Input */
  #input-row { padding: 10px 12px; border-top: 1px solid var(--border); display: flex; gap: 8px; background: var(--bg2); }
  #msg-input { flex: 1; background: var(--bg3); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; font-family: inherit; font-size: 13px; outline: none; }
  #msg-input:focus { border-color: var(--accent); }
  #send-btn { padding: 8px 14px; background: var(--accent); color: #000; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 13px; }
  #send-btn:hover { opacity: 0.85; }
  #no-session { color: var(--muted); font-size: 12px; align-self: center; }

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

  // ── Init ──────────────────────────────────────────────────────────────

  async function init() {
    await refreshSessions();
    setInterval(refreshSessions, 3000);
    setInterval(refreshTasks, 2000);
  }

  // ── Sessions ──────────────────────────────────────────────────────────

  async function refreshSessions() {
    const res = await fetch('/api/sessions');
    sessions = await res.json();
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
    for (const sess of sessions) {
      const tone = sessionTone(sess);
      const div = document.createElement('div');
      div.className = 'session-item' + (sess.id === currentSession ? ' active' : '');
      div.onclick = () => attachSession(sess.id);
      div.innerHTML = `
        <div class="session-name">
          <span class="dot ${tone}"></span>${sess.id}
        </div>
        <div class="session-meta">${sess.entity}</div>
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

    // Clear chat
    const msgs = document.getElementById('messages');
    msgs.innerHTML = '';
    modelState = { state: 'idle', source: null };
    renderServerIndicator();
    renderSessionIndicator();

    // Enable input
    document.getElementById('msg-input').disabled = false;
    document.getElementById('send-btn').disabled = false;

    // Load full history instantly, get current offset
    const histRes = await fetch(`/api/sessions/${id}/history`);
    const { events, offset } = await histRes.json();
    for (const event of events) appendEvent(event);
    syncModelStateFromMeta();
    renderSessionIndicator();

    // SSE from current offset — only new events from here on
    eventSource = new EventSource(`/api/sessions/${id}/events?since=${offset}`);
    const types = ['agent','user','tool','model_status','heartbeat_trigger','heartbeat_finished','status','error'];
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

  // ── Chat ──────────────────────────────────────────────────────────────

  function fmtTime(ts) {
    if (!ts) return '';
    try {
      const d = new Date(ts);
      return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch { return ''; }
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
      renderSessionIndicator();
      renderSessionList();
      return;
    }
    const div = document.createElement('div');
    div.className = `msg ${etype}`;

    let label = '';
    let text = '';

    if (etype === 'agent') {
      label = event.triggered_by === 'heartbeat' ? '⏱ agent' : 'agent';
      div.className += event.triggered_by === 'heartbeat' ? ' heartbeat-agent' : '';
      text = event.content || '';
    } else if (etype === 'user') {
      label = 'you';
      text = event.content || '';
    } else if (etype === 'tool') {
      text = `[tool] ${event.name}(${JSON.stringify(event.input || {})})`;
    } else if (etype === 'heartbeat_trigger') {
      text = '⏱ Heartbeat';
    } else if (etype === 'heartbeat_finished') {
      text = '[session finished — all tasks done]';
    } else if (etype === 'status') {
      text = event.value === 'cancelled' || event.value === 'stopped'
        ? '[server stopped]'
        : `[status: ${event.value}]`;
    } else if (etype === 'error') {
      text = `[error] ${event.content}`;
    } else {
      text = JSON.stringify(event);
    }

    const ts = fmtTime(event.ts);
    const tsHtml = ts ? `<div class="msg-ts">${ts}</div>` : '';

    if (etype === 'status') {
      const prefix = ts ? `<span class="msg-inline-ts">${ts}</span>` : '';
      div.innerHTML = `<div class="msg-inline">${prefix}${escHtml(text)}</div>`;
    } else if (label) {
      div.innerHTML = `<div class="msg-label">${label}</div>${escHtml(text)}${tsHtml}`;
    } else if (etype === 'heartbeat_trigger') {
      div.innerHTML = `${escHtml(text)}${tsHtml}`;
    } else {
      div.innerHTML = `${escHtml(text)}${tsHtml}`;
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
    if (session.status === 'stopped') return 'stopped';
    if (session.pid_alive && session.model_state === 'running') return 'running';
    if (session.has_tasks) return 'queued';
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
    if (meta.status === 'stopped') {
      tone = 'stopped';
      label = 'stopped by user';
    } else if (meta.pid_alive && meta.model_state === 'running') {
      tone = 'running';
      label = `running (${meta.model_source || modelState.source || 'user'})`;
    } else if (meta.has_tasks) {
      tone = 'queued';
      label = 'tasks queued';
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

    await fetch(`/api/sessions/${currentSession}/messages`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ content: text }),
    });
  }

  // ── Tasks ─────────────────────────────────────────────────────────────

  async function refreshTasks() {
    if (!currentSession) return;
    const res = await fetch(`/api/sessions/${currentSession}/tasks`);
    const data = await res.json();
    const content = data.content?.trim() || '(empty)';
    document.getElementById('tasks-content').textContent = content;
    document.getElementById('tasks-textarea').value = data.content || '';
    const meta = currentSessionMeta();
    if (meta) meta.has_tasks = Boolean(data.content?.trim());
    renderServerIndicator();
    renderSessionIndicator();
    renderSessionList();
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
    renderServerIndicator();
    renderSessionIndicator();
  }

  async function startSession() {
    if (!currentSession) return;
    await fetch(`/api/sessions/${currentSession}/start`, { method: 'POST' });
    await refreshSessions();
    renderServerIndicator();
    renderSessionIndicator();
  }

  // ── Utils ─────────────────────────────────────────────────────────────

  function escHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
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
