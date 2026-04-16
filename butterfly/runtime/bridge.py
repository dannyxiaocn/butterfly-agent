"""Butterfly Bridge — unified client-side session handle.

Borrows key patterns from claude-code's replBridge architecture:

  BoundedIDSet    — FIFO-bounded ring buffer for event dedup. Protects against
                    echo (events we sent coming back) and re-delivery on SSE
                    reconnect. O(capacity) memory, O(1) add/has.

  BridgeSession   — Client-side handle for a single session. Abstracts FileIPC
                    into the operations every frontend (web SSE, CLI, WeChat)
                    actually needs:
                      send_message()    — write user_input to context.jsonl
                      send_interrupt()  — write interrupt control event
                      iter_events()     — yield display events with dedup
                      async_wait_for_reply() — poll until matching turn arrives

  interrupt flow  — Frontend writes {"type":"interrupt"} to events.jsonl via
                    send_interrupt(). The session's run_daemon_loop drains any
                    pending inputs and skips the next task tick. A "soft
                    interrupt" — in-progress turns complete; queued work is
                    cleared.

Design notes:
  - File-based, not network-based — same Filesystem-As-Everything principle.
  - BridgeSession wraps FileIPC; frontends never touch FileIPC directly.
  - SSE and polling frontends both use iter_events(); only the delivery
    mechanism (push vs. pull) differs.
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import AsyncIterator, Iterator


# ── BoundedIDSet ──────────────────────────────────────────────────────────────

class BoundedIDSet:
    """FIFO-bounded set backed by a circular buffer.

    Evicts the oldest entry when capacity is reached. Used for two dedup
    roles (mirroring claude-code's BoundedUUIDSet):

      posted_ids   — IDs of events *we* wrote; used to drop echoes if the
                     SSE stream re-delivers our own events (e.g. client
                     reconnects with offset 0).
      seen_ids     — IDs of inbound events already forwarded; guards against
                     duplicate delivery when the client reconnects and the
                     server replays history.

    Capacity 256 covers ~4-5 minutes of typical agent traffic at max tool
    velocity. Tune up if dedup misses are observed.
    """

    def __init__(self, capacity: int = 256) -> None:
        self._capacity = capacity
        self._ring: list[str | None] = [None] * capacity
        self._set: set[str] = set()
        self._write_idx = 0

    def add(self, event_id: str) -> None:
        if event_id in self._set:
            return
        evicted = self._ring[self._write_idx]
        if evicted is not None:
            self._set.discard(evicted)
        self._ring[self._write_idx] = event_id
        self._set.add(event_id)
        self._write_idx = (self._write_idx + 1) % self._capacity

    def has(self, event_id: str) -> bool:
        return event_id in self._set



# ── BridgeSession ─────────────────────────────────────────────────────────────

class BridgeSession:
    """Client-side handle for a single Butterfly session.

    Wraps FileIPC with the higher-level operations that web, CLI, and WeChat
    frontends share. Each frontend creates its own BridgeSession instance;
    state (dedup sets, offsets) is per-instance.

    Args:
        system_dir: Path to _sessions/<session_id>/ (system-only directory).
    """

    def __init__(self, system_dir: Path) -> None:
        from butterfly.runtime.ipc import FileIPC
        self._ipc = FileIPC(system_dir)
        self._seen_ids = BoundedIDSet()   # inbound dedup

    # ── Write ────────────────────────────────────────────────────────────────

    def send_message(
        self,
        content: str,
        *,
        caller: str = "human",
        mode: str = "interrupt",
    ) -> str:
        """Write a user_input event to context.jsonl. Returns the message ID.

        The message ID links the input to the responding turn (user_input_id
        field). Use async_wait_for_reply() with this ID to block until the
        agent finishes.

        Args:
            mode: ``interrupt`` (default) cancels the current agent run if
                any; the new content is merged with the cancelled input when
                the cancelled run had not yet committed an assistant turn.
                ``wait`` queues the message behind in-flight / earlier
                queued work; consecutive wait-mode chat inputs collapse into
                a single user message before the agent fires.
        """
        if mode not in ("interrupt", "wait"):
            raise ValueError(f"invalid mode: {mode!r} (expected interrupt|wait)")
        msg_id = str(uuid.uuid4())
        self._ipc.append_context({
            "type": "user_input",
            "content": content,
            "id": msg_id,
            "caller": caller,
            "mode": mode,
        })
        return msg_id

    def send_interrupt(self) -> None:
        """Write an explicit interrupt control event to events.jsonl.

        The session daemon drains pending queued user_input events and
        cancels the in-progress agent loop. Distinct from ``mode=interrupt``
        on a chat: a chat-with-interrupt cancels then runs the new content;
        a bare interrupt cancels and runs nothing.
        """
        self._ipc.append_event({"type": "interrupt"})

    # ── Read ─────────────────────────────────────────────────────────────────

    def iter_events(
        self,
        context_offset: int = 0,
        events_offset: int = 0,
    ) -> Iterator[tuple[dict, int, int]]:
        """Yield (display_event, new_context_offset, new_events_offset) with dedup.

        Combines context.jsonl and events.jsonl into a single event stream,
        with BoundedIDSet dedup to prevent duplicate delivery on reconnect.

        Typical usage for polling frontends (CLI, WeChat):

            ctx, evt = ipc.context_size(), ipc.events_size()
            while True:
                for event, ctx, evt in bridge.iter_events(ctx, evt):
                    handle(event)
                time.sleep(0.5)

        For SSE frontends (web), use async_iter_events() instead.

        Dedup note: events without an 'id' field are always forwarded.
        Only events carrying 'id' (user_input, turn's user_input_id) are
        run through the dedup filter. This matches the intent — structural
        events (partial_text, tool_call, model_status) are stateless and
        safe to re-deliver; only "record" events need dedup.
        """
        ctx_offset = context_offset
        evt_offset = events_offset

        for event, new_off in self._ipc.tail_context(ctx_offset):
            ctx_offset = new_off
            event_id = event.get("id")
            if event_id:
                if self._seen_ids.has(event_id):
                    continue
                self._seen_ids.add(event_id)
            yield event, ctx_offset, evt_offset

        for event, new_off in self._ipc.tail_runtime_events(evt_offset):
            evt_offset = new_off
            event_id = event.get("id")
            if event_id:
                if self._seen_ids.has(event_id):
                    continue
                self._seen_ids.add(event_id)
            yield event, ctx_offset, evt_offset

    async def async_iter_events(
        self,
        context_offset: int = 0,
        events_offset: int = 0,
        poll_interval: float = 0.3,
    ) -> AsyncIterator[tuple[dict, int, int]]:
        """Async version of iter_events for SSE and async frontends.

        Yields new events as they arrive, sleeping poll_interval between
        checks. Never returns — caller must cancel the task or use an
        outer stop condition.
        """
        ctx = context_offset
        evt = events_offset
        while True:
            had_events = False
            for event, ctx, evt in self.iter_events(ctx, evt):
                had_events = True
                yield event, ctx, evt
            if not had_events:
                await asyncio.sleep(poll_interval)

    async def async_wait_for_reply(
        self,
        msg_id: str,
        timeout: float = 120.0,
        poll_interval: float = 0.5,
    ) -> str | None:
        """Async wait until the agent emits a turn whose user_input_id or merged_user_input_ids contains msg_id.

        Returns the assistant's final text, or None if timeout is reached.
        """
        import json
        import asyncio
        import time
        deadline = time.monotonic() + timeout
        offset = self._ipc.context_size()

        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            if not self._ipc.context_path.exists():
                continue
            with self._ipc.context_path.open("r", encoding="utf-8") as f:
                f.seek(offset)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    offset = f.tell()
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except Exception:
                        continue
                    if event.get("type") != "turn":
                        continue
                    merged_ids = event.get("merged_user_input_ids") or []
                    if event.get("user_input_id") != msg_id and msg_id not in merged_ids:
                        continue
                    for msg in reversed(event.get("messages", [])):
                        if msg.get("role") == "assistant":
                            content = msg.get("content", "")
                            if isinstance(content, str):
                                return content.strip() or None
                            text = next(
                                (b.get("text", "") for b in content
                                 if isinstance(b, dict) and b.get("type") == "text"),
                                "",
                            )
                            return text.strip() or None
        return None

