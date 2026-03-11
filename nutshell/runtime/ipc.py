"""FileIPC: file-based IPC between nutshell daemon and chat UI.

All communication flows through a single append-only context.jsonl file.

Event types written to context.jsonl:
  user_input       — UI → daemon: {"type": "user_input", "content": "...", "id": "...", "ts": "..."}
  turn             — daemon → UI: {"type": "turn", "triggered_by": "user|heartbeat", "messages": [...], "ts": "..."}
  model_status     — daemon → UI: {"type": "model_status", "state": "running|idle", "source": "user|heartbeat", "ts": "..."}
  status           — daemon → UI: {"type": "status", "value": "...", "ts": "..."}
  error            — daemon → UI: {"type": "error", "content": "...", "ts": "..."}
  heartbeat_finished — daemon → UI: {"type": "heartbeat_finished", "ts": "..."}

Display events derived for the UI from context.jsonl:
  user             — from user_input
  agent            — from turn (last assistant message)
  tool             — from turn (tool_use blocks in assistant messages)
  model_status     — passed through as-is
  heartbeat_trigger — from turn with triggered_by="heartbeat"
  heartbeat_finished, status, error — passed through as-is
"""
from __future__ import annotations
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterator


def _to_display_events(event: dict) -> list[dict]:
    """Convert a single context.jsonl event to zero or more UI display events."""
    etype = event.get("type")
    ts = event.get("ts", "")

    if etype == "user_input":
        return [{"type": "user", "content": event.get("content", ""), "ts": ts}]

    if etype == "turn":
        result: list[dict] = []
        triggered_by = event.get("triggered_by", "user")
        if triggered_by == "heartbeat":
            result.append({"type": "heartbeat_trigger", "ts": ts})
        # Tool calls from assistant messages
        for msg in event.get("messages", []):
            if msg["role"] == "assistant":
                content = msg["content"]
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            result.append({"type": "tool", "name": block["name"], "input": block.get("input", {}), "ts": ts})
        # Final assistant text (last assistant message)
        for msg in reversed(event.get("messages", [])):
            if msg["role"] == "assistant":
                content = msg["content"]
                text = content if isinstance(content, str) else next(
                    (b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"), ""
                )
                if text:
                    ev: dict = {"type": "agent", "content": text, "ts": ts}
                    if triggered_by == "heartbeat":
                        ev["triggered_by"] = "heartbeat"
                    result.append(ev)
                break
        return result

    if etype in ("model_status", "heartbeat_finished", "status", "error"):
        return [event]

    return []


class FileIPC:
    """File-based IPC for a single session via a single append-only context.jsonl.

    Layout inside session_dir/:
        context.jsonl — all events (user_input, turn, status, error, heartbeat_finished)
    """

    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self.context_path = session_dir / "context.jsonl"

    # ── Write ────────────────────────────────────────────────────────

    def append(self, event: dict) -> None:
        """Append an event to context.jsonl (always O(1))."""
        event.setdefault("ts", datetime.now().isoformat())
        with self.context_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def send_message(self, content: str, msg_id: str | None = None) -> str:
        """Append a user_input event. Returns message id."""
        msg_id = msg_id or str(uuid.uuid4())
        self.append({"type": "user_input", "content": content, "id": msg_id})
        return msg_id

    # ── Daemon-side read ──────────────────────────────────────────────

    def poll_inputs(self, offset: int) -> tuple[list[dict], int]:
        """Read new user_input events from context.jsonl starting at byte offset.

        Returns (user_input_events, new_offset).
        The daemon uses this to discover incoming user messages.
        """
        if not self.context_path.exists():
            return [], offset
        with self.context_path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            data = f.read()
            new_offset = f.tell()
        events: list[dict] = []
        for line in data.splitlines():
            line = line.strip()
            if line:
                try:
                    event = json.loads(line)
                    if event.get("type") == "user_input":
                        events.append(event)
                except json.JSONDecodeError:
                    pass
        return events, new_offset

    # ── UI-side read ─────────────────────────────────────────────────

    def tail_display(self, offset: int = 0) -> Iterator[tuple[dict, int]]:
        """Yield (display_event, line_end_offset) from context.jsonl starting at offset.

        Each raw event may expand to multiple display events (e.g. a turn with
        tool calls yields tool events + an agent event). All share the same
        line_end_offset so the UI can resume correctly.
        """
        if not self.context_path.exists():
            return
        with self.context_path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            while True:
                line = f.readline()
                if not line:
                    break
                line_end = f.tell()
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    for display in _to_display_events(event):
                        yield display, line_end
                except json.JSONDecodeError:
                    pass

    def size(self) -> int:
        """Current context.jsonl size in bytes."""
        if not self.context_path.exists():
            return 0
        return self.context_path.stat().st_size
