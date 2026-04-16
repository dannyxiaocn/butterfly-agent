"""FileIPC: file-based IPC between butterfly daemon and chat UI.

Two files per session:

  context.jsonl  — PURE conversation history (user_input + turn only).
                   Sole purpose: restore the full conversation so the daemon
                   can reconstruct agent._history and send correct context to
                   Claude on every new run. Nothing else lives here.

  events.jsonl   — ALL runtime / UI signalling events:
                   model_status, partial_text, tool_call, task_wakeup,
                   task_finished, status, error.
                   Consumed by the SSE stream; never used by load_history().

context.jsonl event types:
  user_input   — UI → daemon: {"type": "user_input", "content": "...", "id": "...",
                               "ts": "...", "mode": "interrupt|wait" (optional, default
                               via pending_inputs.default_mode_for_source: user/panel
                               → interrupt, task → wait)}
  turn         — daemon → UI: {"type": "turn", "triggered_by": "user|task:<name>",
                               "messages": [...], "ts": "...",
                               "merged_user_input_ids": [...] (optional; set when more
                               than one user_input event was merged into the turn)}

events.jsonl event types:
  model_status       — {"type": "model_status", "state": "running|idle", "source": "...", "ts": "..."}
  partial_text       — {"type": "partial_text", "content": "...", "ts": "..."}
  tool_call          — {"type": "tool_call", "name": "...", "input": {...}, "ts": "..."}
  tool_done          — {"type": "tool_done", "name": "...", "result_len": 123, "ts": "..."}
  thinking_start     — {"type": "thinking_start", "block_id": "th:...", "ts": "..."}
  thinking_done      — {"type": "thinking_done", "block_id": "th:...", "text": "...", "duration_ms": 1234, "ts": "..."}
  loop_start         — {"type": "loop_start", "ts": "..."}
  loop_end           — {"type": "loop_end", "iterations": 2, "usage": {...}, "ts": "..."}
  task_wakeup        — {"type": "task_wakeup", "card": "...", "ts": "..."}
  task_finished       — {"type": "task_finished", "card": "...", "ts": "..."}
  status             — {"type": "status", "value": "...", "ts": "..."}
  error              — {"type": "error", "content": "...", "ts": "..."}
  system_notice      — {"type": "system_notice", "message": "...", "meta_version": "...", "session_version": "...", "ts": "..."}

Display events derived for the UI:
  user             — from user_input (context)
  agent            — from turn last assistant message (context)
  tool             — from turn tool_use blocks (context) OR streaming tool_call (events)
  model_status, partial_text, tool_done, loop_start, loop_end,
  task_wakeup, task_finished, status, error — pass-through (events)
"""
from __future__ import annotations
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterator


# ── Display event converters ──────────────────────────────────────────────────

def _context_event_to_display(event: dict, *, for_history: bool = False) -> list[dict]:
    """Convert a context.jsonl event (user_input or turn) to display events.

    Args:
        for_history: When True (history endpoint), always emit tools
                     from turn content — the streaming events
                     in events.jsonl are not available for history replay.
                     When False (SSE live tail), respect has_streaming_tools /
                     pre_triggered flags to avoid duplicating live-streamed items.
    """
    etype = event.get("type")
    ts = event.get("ts", "")

    if etype == "user_input":
        display = {"type": "user", "content": event.get("content", ""), "ts": ts}
        if not for_history and event.get("id"):
            display["id"] = event["id"]
        return [display]

    if etype == "turn":
        result: list[dict] = []
        triggered_by = event.get("triggered_by", "user")

        # Tool calls: always emit for history; for SSE only when NOT already
        # streamed live via tool_call events (has_streaming_tools flag).
        if for_history or not event.get("has_streaming_tools"):
            for msg in event.get("messages", []):
                if msg["role"] == "assistant":
                    content = msg["content"]
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                block_ts = block.get("ts", ts)
                                result.append({
                                    "type": "tool",
                                    "name": block["name"],
                                    "input": block.get("input", {}),
                                    "ts": block_ts,
                                })

        # Thinking content:
        #   * For history replay: always emit so the transcript includes
        #     prior turns' reasoning.
        #   * For live SSE: when the turn was streamed with the new
        #     thinking_start/thinking_done events, don't re-emit from the
        #     serialized turn content — the live events already rendered the
        #     cells and re-emitting would duplicate them.
        if for_history or not event.get("has_streaming_thinking"):
            thinking_idx = 0
            for msg in event.get("messages", []):
                if msg["role"] == "assistant":
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "thinking":
                                thinking_text = block.get("thinking", "")
                                if thinking_text:
                                    thinking_ev: dict = {"type": "thinking", "content": thinking_text, "ts": ts}
                                    # Always set id (not guarded by for_history) so thinking events
                                    # returned by the history endpoint can also be deduped client-side,
                                    # preventing repeat renders on visibilitychange.
                                    thinking_ev["id"] = f"thinking:{ts}:{thinking_idx}"
                                    thinking_idx += 1
                                    result.append(thinking_ev)

        # Final assistant text (last assistant message)
        for msg in reversed(event.get("messages", [])):
            if msg["role"] == "assistant":
                content = msg["content"]
                text = content if isinstance(content, str) else next(
                    (b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"), ""
                )
                if text:
                    ev: dict = {"type": "agent", "content": text, "ts": ts}
                    if not for_history:
                        # Use ts-based id (NOT user_input_id) so server-side BoundedIDSet
                        # does not dedup the agent event after seeing the user event with
                        # the same user_input_id.
                        ev["id"] = f"turn:{ts}"
                    if event.get("usage"):
                        ev["usage"] = event["usage"]
                    result.append(ev)
                break

        return result

    # Anything else (old-format context.jsonl with mixed events) — silently ignored.
    return []


def _runtime_event_to_display(event: dict) -> list[dict]:
    """Convert an events.jsonl event to display events.

    All runtime events pass through or are transformed for the SSE stream.
    """
    etype = event.get("type")
    ts = event.get("ts", "")

    if etype == "partial_text":
        return [{"type": "partial_text", "content": event.get("content", ""), "ts": ts}]

    if etype == "tool_call":
        return [{"type": "tool", "name": event.get("name"), "input": event.get("input", {}), "ts": ts}]

    if etype in (
        "model_status",
        "tool_done",
        "thinking_start",
        "thinking_done",
        "loop_start",
        "loop_end",
        "task_wakeup",
        "task_finished",
        "status",
        "error",
        "system_notice",
    ):
        return [event]

    return []


# ── FileIPC ───────────────────────────────────────────────────────────────────

class FileIPC:
    """File-based IPC for a single session.

    Two files:
        context.jsonl — conversation history only (user_input, turn)
        events.jsonl  — runtime/UI events (model_status, partial_text, etc.)
    """

    def __init__(self, system_dir: Path) -> None:
        self.system_dir = system_dir
        self.context_path = system_dir / "context.jsonl"
        self.events_path = system_dir / "events.jsonl"

    # ── Write ────────────────────────────────────────────────────────────────

    def append_context(self, event: dict) -> None:
        """Append a conversation event (user_input or turn) to context.jsonl."""
        event.setdefault("ts", datetime.now().isoformat())
        with self.context_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def append_event(self, event: dict) -> None:
        """Append a runtime/UI event to events.jsonl."""
        event.setdefault("ts", datetime.now().isoformat())
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def send_message(self, content: str, msg_id: str | None = None) -> str:
        """Append a user_input event to context.jsonl. Returns message id."""
        msg_id = msg_id or str(uuid.uuid4())
        self.append_context({"type": "user_input", "content": content, "id": msg_id})
        return msg_id

    def send_interrupt(self) -> None:
        """Append an interrupt control event to events.jsonl.

        The session's run_daemon_loop polls for this via poll_interrupt() and
        responds with a soft interrupt: drains pending inputs and skips the
        next task tick.
        """
        self.append_event({"type": "interrupt"})

    # ── Daemon-side read ─────────────────────────────────────────────────────

    def poll_interrupt(self, offset: int) -> tuple[bool, int]:
        """Read events.jsonl for an 'interrupt' event at or after offset.

        Returns (found, new_offset). The daemon calls this each cycle; when
        found=True it should drain pending inputs, skip the next task tick,
        and emit {"type": "interrupted"} back to events.jsonl.
        """
        if not self.events_path.exists():
            return False, offset
        found = False
        with self.events_path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            data = f.read()
            new_offset = f.tell()
        for line in data.splitlines():
            line = line.strip()
            if line:
                try:
                    event = json.loads(line)
                    if event.get("type") == "interrupt":
                        found = True
                except json.JSONDecodeError:
                    pass
        return found, new_offset

    def poll_inputs(self, offset: int) -> tuple[list[dict], int]:
        """Read new user_input events from context.jsonl starting at byte offset.

        Returns (user_input_events, new_offset).
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

    # ── UI-side read ─────────────────────────────────────────────────────────

    def _readline_loop(
        self, path: Path, offset: int, converter
    ) -> Iterator[tuple[dict, int]]:
        """Shared readline loop: yield (display_event, line_end_offset) from path."""
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
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
                    for display in converter(event):
                        yield display, line_end
                except json.JSONDecodeError:
                    pass

    def tail_history(self, offset: int = 0) -> Iterator[tuple[dict, int]]:
        """Yield display events from context.jsonl for the history endpoint.

        Always emits tools from turn content (for_history=True),
        since events.jsonl is not consulted for history replay.
        """
        yield from self._readline_loop(
            self.context_path, offset,
            lambda e: _context_event_to_display(e, for_history=True),
        )

    def tail_context(self, offset: int = 0) -> Iterator[tuple[dict, int]]:
        """Yield display events from context.jsonl for the live SSE stream.

        Respects has_streaming_tools / pre_triggered flags to avoid duplicating
        items already delivered via the events.jsonl stream.
        """
        yield from self._readline_loop(
            self.context_path, offset,
            lambda e: _context_event_to_display(e, for_history=False),
        )

    def tail_runtime_events(self, offset: int = 0) -> Iterator[tuple[dict, int]]:
        """Yield display events from events.jsonl for the live SSE stream."""
        yield from self._readline_loop(
            self.events_path, offset, _runtime_event_to_display,
        )

    def context_size(self) -> int:
        """Current context.jsonl size in bytes (used to initialize poll_inputs offset)."""
        if not self.context_path.exists():
            return 0
        return self.context_path.stat().st_size

    def events_size(self) -> int:
        """Current events.jsonl size in bytes (used to initialize SSE events offset)."""
        if not self.events_path.exists():
            return 0
        return self.events_path.stat().st_size

    def last_running_event_offset(self) -> int:
        """Byte offset of the last model_status:running line in events.jsonl.

        Used by the history endpoint when the session is actively running:
        returning this offset as events_since lets the SSE stream replay the
        in-progress turn (model_status:running + partial_text chunks) so a
        re-attaching client immediately sees the streaming state.

        Returns events_size() if:
        - No running event is found (safe default), OR
        - A model_status:idle event follows the last running event (turn already
          completed — replaying would cause duplicate tool events in the UI).
        """
        if not self.events_path.exists():
            return 0
        last_offset = -1
        has_idle_after = False
        with self.events_path.open("r", encoding="utf-8") as f:
            while True:
                line_start = f.tell()
                line = f.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    ev = json.loads(stripped)
                    if ev.get("type") == "model_status":
                        if ev.get("state") == "running":
                            last_offset = line_start
                            has_idle_after = False  # reset on each new running event
                        elif ev.get("state") == "idle" and last_offset >= 0:
                            has_idle_after = True
                except Exception:
                    pass
        if last_offset < 0 or has_idle_after:
            # No running event, or turn already completed — no replay needed
            return self.events_size()
        return last_offset
