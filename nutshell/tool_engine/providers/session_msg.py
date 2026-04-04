"""send_to_session — built-in tool for agent-to-agent messaging via QjbQ session relay.

This replaces the older direct context.jsonl mutation path with a single
canonical relay: QjbQ writes inbound user_input events into the target session's
_system context, and sync callers poll the same context for the matching turn.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid as _uuid_mod
from datetime import datetime
from pathlib import Path

import httpx

# Allow uuid to be overridden in tests
uuid = _uuid_mod

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_DEFAULT_SYSTEM_BASE = _REPO_ROOT / "_sessions"
_DEFAULT_QJBQ_BASE_URL = os.environ.get("QJBQ_BASE_URL", "http://127.0.0.1:8081")
_POLL_INTERVAL = 0.5




def _append_jsonl(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _find_turn(ctx_path: Path, msg_id: str) -> str | None:
    """Scan context.jsonl for a turn with user_input_id == msg_id.

    Returns the last assistant text if found, else None.
    Returns empty string if the turn exists but has no text.
    """
    if not ctx_path.exists():
        return None
    try:
        with ctx_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "turn":
                    continue
                if event.get("user_input_id") != msg_id:
                    continue
                for msg in reversed(event.get("messages", [])):
                    if msg.get("role") == "assistant":
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            return content
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    return block.get("text", "")
                return ""
    except Exception:
        return None
    return None


async def send_to_session(
    *,
    session_id: str,
    message: str,
    mode: str = "sync",
    timeout: float = 60.0,
    _system_base: Path | None = None,
    _qjbq_base_url: str | None = None,
) -> str:
    """Send a message to another Nutshell session.

    Uses QjbQ as the canonical inter-session messaging path.

    Args:
        session_id: Target session ID.
        message: Message content to send.
        mode: "sync" (wait for reply) or "async" (fire-and-forget).
        timeout: Max seconds to wait in sync mode.
        _system_base: Override _sessions/ directory (for testing / polling).
        _qjbq_base_url: Override QjbQ base URL (for testing).

    Returns:
        In sync mode: the agent's response text, or an error string.
        In async mode: confirmation string.
    """
    system_base = _system_base if _system_base is not None else _DEFAULT_SYSTEM_BASE
    target_dir = system_base / session_id

    current_sid = os.environ.get("NUTSHELL_SESSION_ID", "")
    if current_sid and current_sid == session_id:
        return f"Error: cannot send to own session ({session_id})."

    if not (target_dir / "manifest.json").exists():
        return f"Error: session '{session_id}' not found."

    msg_id = str(uuid.uuid4())
    qjbq_base_url = (_qjbq_base_url or _DEFAULT_QJBQ_BASE_URL).rstrip("/")

    event = {
        "session_id": session_id,
        "content": message,
        "message_id": msg_id,
        "caller": "agent",
        "mode": mode,
        "ts": datetime.now().isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{qjbq_base_url}/api/session-message",
                json=event,
            )
        if resp.status_code != 200:
            raise httpx.HTTPStatusError("relay rejected message", request=resp.request if hasattr(resp, 'request') else None, response=resp)
    except Exception:
        # Backward-compatible fallback while migrating to QjbQ as the canonical path:
        # if the relay is unavailable, still write the inbound event directly.
        _append_jsonl(target_dir / "context.jsonl", {
            "type": "user_input",
            "content": message,
            "id": msg_id,
            "caller": "agent",
            "mode": mode,
            "ts": event["ts"],
        })

    if mode == "async":
        return f"Message sent to session {session_id}."

    ctx_path = target_dir / "context.jsonl"
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        reply = _find_turn(ctx_path, msg_id)
        if reply is not None:
            return reply
        await asyncio.sleep(_POLL_INTERVAL)

    return f"Timeout: no response from session '{session_id}' within {timeout:.0f}s."

def _extract_error_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:
        text = resp.text.strip()
        return text or f"HTTP {resp.status_code}"
    detail = data.get("detail") if isinstance(data, dict) else None
    return str(detail or f"HTTP {resp.status_code}")
