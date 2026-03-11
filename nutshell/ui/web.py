"""Nutshell Web UI — FastAPI server with SSE streaming.

Browser connects via SSE to receive real-time agent output; sends messages
via POST. FastAPI is a thin HTTP wrapper over FileIPC — no agent logic here.

Usage:
    nutshell-web
    nutshell-web --port 8080 --instances-dir ./instances
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

INSTANCES_DIR = Path("instances")
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


# ── FastAPI app ────────────────────────────────────────────────────────────

def create_app(instances_dir: Path) -> FastAPI:
    app = FastAPI(title="Nutshell Web UI", docs_url=None, redoc_url=None)

    # ── HTML ──────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return _HTML

    # ── Instances ─────────────────────────────────────────────────────────

    @app.get("/api/instances")
    async def list_instances():
        if not instances_dir.exists():
            return []
        result = []
        for d in sorted(instances_dir.iterdir()):
            if not d.is_dir():
                continue
            manifest_path = d / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                manifest = {}
            is_stopped = manifest.get("status") == "stopped"
            kanban_path = d / "kanban.md"
            has_kanban = kanban_path.exists() and bool(kanban_path.read_text(encoding="utf-8").strip())
            result.append({
                "id": d.name,
                "entity": manifest.get("entity", "?"),
                "created_at": manifest.get("created_at", ""),
                # Green only when daemon is running, not stopped, and has pending work
                "alive": _pid_alive(manifest.get("pid")) and not is_stopped and has_kanban,
            })
        return result

    @app.post("/api/instances")
    async def create_instance(body: dict):
        instance_id = body.get("id") or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        entity = body.get("entity", _DEFAULT_ENTITY)
        heartbeat = float(body.get("heartbeat", 10.0))

        instance_dir = instances_dir / instance_id
        instance_dir.mkdir(parents=True, exist_ok=True)
        (instance_dir / "files").mkdir(exist_ok=True)
        (instance_dir / "context.jsonl").touch(exist_ok=True)

        manifest = {
            "instance_id": instance_id,
            "entity": entity,
            "created_at": datetime.now().isoformat(),
            "heartbeat": heartbeat,
        }
        (instance_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return {"id": instance_id, "entity": entity}

    # ── Messages ──────────────────────────────────────────────────────────

    @app.post("/api/instances/{instance_id}/messages")
    async def send_message(instance_id: str, body: dict):
        from nutshell.core.ipc import FileIPC
        instance_dir = instances_dir / instance_id
        if not instance_dir.exists():
            raise HTTPException(404, f"Instance not found: {instance_id}")
        ipc = FileIPC(instance_dir)
        msg_id = ipc.send_message(body.get("content", ""))
        return {"id": msg_id}

    # ── SSE events ────────────────────────────────────────────────────────

    @app.get("/api/instances/{instance_id}/events")
    async def stream_events(instance_id: str, since: int = 0):
        instance_dir = instances_dir / instance_id
        if not instance_dir.exists():
            raise HTTPException(404, f"Instance not found: {instance_id}")

        async def generator() -> AsyncIterator[str]:
            from nutshell.core.ipc import FileIPC
            ipc = FileIPC(instance_dir)
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

    @app.get("/api/instances/{instance_id}/history")
    async def get_history(instance_id: str):
        """Return all display events derived from context.jsonl + current byte offset.

        JS loads this once on attach to render full history instantly,
        then starts SSE from the returned offset for new events only.
        """
        from nutshell.core.ipc import FileIPC
        ipc = FileIPC(instances_dir / instance_id)
        events: list[dict] = []
        size = 0
        for event, offset in ipc.tail_display(0):
            events.append(event)
            size = offset
        return {"events": events, "offset": size}

    # ── Stop / Start ──────────────────────────────────────────────────────

    @app.post("/api/instances/{instance_id}/stop")
    async def stop_instance(instance_id: str):
        from nutshell.core.ipc import FileIPC
        instance_dir = instances_dir / instance_id
        _set_manifest_status(instance_dir / "manifest.json", "stopped")
        if instance_dir.exists():
            FileIPC(instance_dir).append(
                {"type": "status", "value": "heartbeat paused — use ▶ Start to resume"}
            )
        return {"ok": True}

    @app.post("/api/instances/{instance_id}/start")
    async def start_instance(instance_id: str):
        from nutshell.core.ipc import FileIPC
        instance_dir = instances_dir / instance_id
        _set_manifest_status(instance_dir / "manifest.json", "active")
        if instance_dir.exists():
            FileIPC(instance_dir).append(
                {"type": "status", "value": "heartbeat resumed"}
            )
        return {"ok": True}

    # ── Kanban ────────────────────────────────────────────────────────────

    @app.get("/api/instances/{instance_id}/kanban")
    async def get_kanban(instance_id: str):
        kanban_path = instances_dir / instance_id / "kanban.md"
        if not kanban_path.exists():
            return {"content": ""}
        return {"content": kanban_path.read_text(encoding="utf-8")}

    @app.put("/api/instances/{instance_id}/kanban")
    async def set_kanban(instance_id: str, body: dict):
        instance_dir = instances_dir / instance_id
        if not instance_dir.exists():
            raise HTTPException(404, f"Instance not found: {instance_id}")
        kanban_path = instance_dir / "kanban.md"
        kanban_path.write_text(body.get("content", ""), encoding="utf-8")
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
  #server-status { font-size: 11px; color: var(--muted); margin-left: auto; }
  #server-status.alive { color: var(--green); }

  /* Layout */
  #main { display: flex; flex: 1; overflow: hidden; }
  #sidebar { width: 220px; background: var(--bg2); border-right: 1px solid var(--border); display: flex; flex-direction: column; }
  #chat-area { flex: 1; display: flex; flex-direction: column; }
  #kanban-panel { width: 240px; background: var(--bg2); border-left: 1px solid var(--border); display: flex; flex-direction: column; }

  /* Sidebar */
  .panel-header { padding: 10px 12px; font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
  #instance-list { flex: 1; overflow-y: auto; }
  .instance-item { padding: 8px 12px; cursor: pointer; border-bottom: 1px solid var(--border); transition: background 0.1s; }
  .instance-item:hover { background: var(--bg3); }
  .instance-item.active { background: var(--bg3); border-left: 2px solid var(--accent); }
  .instance-name { font-weight: 500; color: var(--text); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .instance-meta { font-size: 10px; color: var(--muted); margin-top: 2px; }
  .dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }
  .dot.alive { background: var(--green); }
  .dot.dead  { background: var(--muted); }
  #new-btn { margin: 10px 10px 0; padding: 6px 10px; background: var(--accent); color: #000; border: none; border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 600; }
  #new-btn:hover { opacity: 0.85; }
  .instance-controls { display: flex; gap: 4px; margin: 4px 10px 10px; }
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

  /* Input */
  #input-row { padding: 10px 12px; border-top: 1px solid var(--border); display: flex; gap: 8px; background: var(--bg2); }
  #msg-input { flex: 1; background: var(--bg3); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; font-family: inherit; font-size: 13px; outline: none; }
  #msg-input:focus { border-color: var(--accent); }
  #send-btn { padding: 8px 14px; background: var(--accent); color: #000; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 13px; }
  #send-btn:hover { opacity: 0.85; }
  #no-instance { color: var(--muted); font-size: 12px; align-self: center; }

  /* Kanban */
  #kanban-content { flex: 1; padding: 10px 12px; overflow-y: auto; white-space: pre-wrap; font-size: 12px; color: var(--text); line-height: 1.6; }
  #kanban-edit { display: none; flex-direction: column; flex: 1; padding: 8px; gap: 6px; }
  #kanban-textarea { flex: 1; background: var(--bg3); color: var(--text); border: 1px solid var(--border); border-radius: 4px; padding: 6px; font-family: inherit; font-size: 12px; resize: none; outline: none; }
  #kanban-save { padding: 4px 10px; background: var(--green); color: #000; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; }
  #kanban-cancel { padding: 4px 10px; background: var(--bg3); color: var(--muted); border: 1px solid var(--border); border-radius: 4px; cursor: pointer; font-size: 12px; }
  .kanban-actions { display: flex; gap: 6px; }
  #kanban-edit-btn { cursor: pointer; font-size: 11px; color: var(--muted); border: none; background: none; }
  #kanban-edit-btn:hover { color: var(--accent); }
</style>
</head>
<body>

<div id="header">
  <h1>🥜 nutshell</h1>
  <span id="server-status">checking server...</span>
</div>

<div id="main">
  <!-- Sidebar: instances -->
  <div id="sidebar">
    <div class="panel-header">
      Instances
      <button id="new-btn" onclick="showNewInstanceDialog()">+ New</button>
    </div>
    <div id="instance-list"></div>
    <div class="instance-controls">
      <button class="ctrl-btn stop" onclick="stopInstance()">⏸ Stop</button>
      <button class="ctrl-btn start" onclick="startInstance()">▶ Start</button>
    </div>
  </div>

  <!-- Chat -->
  <div id="chat-area">
    <div id="messages">
      <div class="msg status">Select or create an instance to start chatting.</div>
    </div>
    <div id="input-row">
      <input id="msg-input" type="text" placeholder="Type a message..." disabled onkeydown="onInputKey(event)">
      <button id="send-btn" onclick="sendMessage()" disabled>Send</button>
    </div>
  </div>

  <!-- Kanban -->
  <div id="kanban-panel">
    <div class="panel-header">
      Kanban
      <button id="kanban-edit-btn" onclick="toggleKanbanEdit()">edit</button>
    </div>
    <div id="kanban-content">(no instance selected)</div>
    <div id="kanban-edit">
      <textarea id="kanban-textarea" placeholder="Add tasks here..."></textarea>
      <div class="kanban-actions">
        <button id="kanban-save" onclick="saveKanban()">Save</button>
        <button id="kanban-cancel" onclick="toggleKanbanEdit()">Cancel</button>
      </div>
    </div>
  </div>
</div>

<script>
  let currentInstance = null;
  let eventSource = null;
  let instances = [];

  // ── Init ──────────────────────────────────────────────────────────────

  async function init() {
    await refreshInstances();
    setInterval(refreshInstances, 3000);
    setInterval(refreshKanban, 2000);
  }

  // ── Instances ─────────────────────────────────────────────────────────

  async function refreshInstances() {
    const res = await fetch('/api/instances');
    instances = await res.json();
    renderInstanceList();

    const alive = instances.some(i => i.alive);
    const el = document.getElementById('server-status');
    el.textContent = alive ? '● server running' : '○ server stopped';
    el.className = alive ? 'alive' : '';
  }

  function renderInstanceList() {
    const list = document.getElementById('instance-list');
    list.innerHTML = '';
    for (const inst of instances) {
      const div = document.createElement('div');
      div.className = 'instance-item' + (inst.id === currentInstance ? ' active' : '');
      div.onclick = () => attachInstance(inst.id);
      div.innerHTML = `
        <div class="instance-name">
          <span class="dot ${inst.alive ? 'alive' : 'dead'}"></span>${inst.id}
        </div>
        <div class="instance-meta">${inst.entity}</div>
      `;
      list.appendChild(div);
    }
  }

  async function attachInstance(id) {
    if (id === currentInstance) return;
    currentInstance = id;
    renderInstanceList();

    // Close old SSE
    if (eventSource) { eventSource.close(); eventSource = null; }

    // Clear chat
    const msgs = document.getElementById('messages');
    msgs.innerHTML = '';

    // Enable input
    document.getElementById('msg-input').disabled = false;
    document.getElementById('send-btn').disabled = false;

    // Load full history instantly from outbox, get current offset
    const histRes = await fetch(`/api/instances/${id}/history`);
    const { events, offset } = await histRes.json();
    for (const event of events) appendEvent(event);

    // SSE from current offset — only new events from here on
    eventSource = new EventSource(`/api/instances/${id}/events?since=${offset}`);
    const types = ['agent','user','tool','heartbeat_trigger','heartbeat_finished','status','error'];
    for (const t of types) {
      eventSource.addEventListener(t, e => appendEvent(JSON.parse(e.data)));
    }

    refreshKanban();
  }

  async function showNewInstanceDialog() {
    const id = prompt('Instance ID (leave empty for timestamp):') ?? null;
    if (id === null) return;
    const entity = prompt('Entity path:', 'entity/agent_core');
    if (!entity) return;
    const res = await fetch('/api/instances', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ id: id || '', entity }),
    });
    const data = await res.json();
    await refreshInstances();
    attachInstance(data.id);
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
      text = '[instance finished — all tasks done]';
    } else if (etype === 'status') {
      text = `[status: ${event.value}]`;
    } else if (etype === 'error') {
      text = `[error] ${event.content}`;
    } else {
      text = JSON.stringify(event);
    }

    const ts = fmtTime(event.ts);
    const tsHtml = ts ? `<div class="msg-ts">${ts}</div>` : '';

    if (label) {
      div.innerHTML = `<div class="msg-label">${label}</div>${escHtml(text)}${tsHtml}`;
    } else if (etype === 'heartbeat_trigger') {
      div.innerHTML = `${escHtml(text)}${tsHtml}`;
    } else {
      div.innerHTML = `${escHtml(text)}${tsHtml}`;
    }

    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
  }

  function onInputKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  }

  async function sendMessage() {
    const input = document.getElementById('msg-input');
    const text = input.value.trim();
    if (!text || !currentInstance) return;
    input.value = '';

    await fetch(`/api/instances/${currentInstance}/messages`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ content: text }),
    });
  }

  // ── Kanban ────────────────────────────────────────────────────────────

  async function refreshKanban() {
    if (!currentInstance) return;
    const res = await fetch(`/api/instances/${currentInstance}/kanban`);
    const data = await res.json();
    const content = data.content?.trim() || '(empty)';
    document.getElementById('kanban-content').textContent = content;
    document.getElementById('kanban-textarea').value = data.content || '';
  }

  function toggleKanbanEdit() {
    const view = document.getElementById('kanban-content');
    const edit = document.getElementById('kanban-edit');
    const isEditing = edit.style.display === 'flex';
    view.style.display = isEditing ? 'block' : 'none';
    edit.style.display = isEditing ? 'none' : 'flex';
  }

  async function saveKanban() {
    if (!currentInstance) return;
    const content = document.getElementById('kanban-textarea').value;
    await fetch(`/api/instances/${currentInstance}/kanban`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ content }),
    });
    toggleKanbanEdit();
    refreshKanban();
  }

  // ── Stop / Start ──────────────────────────────────────────────────────

  async function stopInstance() {
    if (!currentInstance) return;
    await fetch(`/api/instances/${currentInstance}/stop`, { method: 'POST' });
    await refreshInstances(); // immediately reflect stopped state in indicator
  }

  async function startInstance() {
    if (!currentInstance) return;
    await fetch(`/api/instances/${currentInstance}/start`, { method: 'POST' });
    await refreshInstances(); // immediately reflect resumed state in indicator
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
    parser.add_argument("--instances-dir", default=str(INSTANCES_DIR), metavar="DIR")
    args = parser.parse_args()

    instances_dir = Path(args.instances_dir)
    instances_dir.mkdir(parents=True, exist_ok=True)

    app = create_app(instances_dir)
    print(f"nutshell web UI: http://localhost:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
