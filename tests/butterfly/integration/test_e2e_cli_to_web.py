"""End-to-end alignment: IO write path → SSE → display history → Card reducer.

Verifies the core invariant: **live SSE and history replay emit the exact
same event payloads for the same event**, and therefore the exact same
sequence of Cards a frontend reducer would produce.

Flow, all against a `TestClient`-backed FastAPI app:

    1. Create a session via `io.create_session`.
    2. Send a user message via `io.send_message`.
    3. Directly `append_event` a canonical agent sequence: thinking →
       text → tool_call → tool_result → text. This stands in for a full
       provider round-trip without standing up a real Session daemon.
    4. Collect the SSE stream frames (via `/events/stream`).
    5. Collect the display-history JSON (via `/history`).
    6. Assert the two lists are identical, modulo SSE framing.
    7. Apply the same pure reducer rules to both streams and assert the
       resulting Card sequence is identical.

Single money-path test; shape-level pins live in `test_events.py` /
`test_llm_context.py` / `test_app.py`.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from butterfly.runtime import io
from butterfly.runtime.events import (
    EVENT_AGENT_TEXT,
    EVENT_AGENT_THINKING,
    EVENT_AGENT_TOOL_CALL,
    EVENT_AGENT_TOOL_RESULT,
    EVENT_USER_INPUT,
    append_event,
    read_events,
)
from ui.web.app import create_app


# ── Helpers ─────────────────────────────────────────────────────────────

def _seed_manifest(system_dir: Path, session_id: str) -> None:
    """Write the minimum manifest so service-layer helpers don't 404.

    ``io.create_session`` uses the service layer which expects an
    ``agenthub/`` dir for the named agent. For this alignment test we
    skip create_session and hand-seed so the rest of the contract (send
    + append + SSE + history) is what's under test.
    """
    system_dir.mkdir(parents=True, exist_ok=True)
    (system_dir / "manifest.json").write_text(
        json.dumps({
            "session_id": session_id,
            "agent": "agent",
            "created_at": "2026-01-01T00:00:00",
        }),
        encoding="utf-8",
    )


def _setup_session(root: Path, session_id: str = "e2e") -> Path:
    """Create the on-disk session shell used by every test in this file."""
    sessions_dir = root / "sessions"
    system_dir = root / "_sessions" / session_id
    (sessions_dir / session_id / "core" / "tasks").mkdir(parents=True)
    _seed_manifest(system_dir, session_id)
    return root


async def _collect_sse_events(
    app,
    url: str,
    *,
    expected_frames: int,
    timeout: float = 5.0,
) -> list[dict]:
    """Drive the ASGI app until ``expected_frames`` real SSE frames land,
    then fire ``http.disconnect`` to cleanly stop the stream generator.

    Pattern adapted from ``tests/ui/web/test_app.py`` — httpx's TestClient
    buffers SSE past the first yield, so we speak ASGI directly. Returns
    the decoded event dicts (the data-line JSON payload on each frame).
    """
    assert url.startswith("/")
    path, _, query = url.partition("?")
    received: list[dict] = []
    disconnect_sent = asyncio.Event()
    frame_count = [0]

    async def receive() -> dict:
        if not disconnect_sent.is_set():
            await asyncio.sleep(0)  # let the server run
            if not disconnect_sent.is_set():
                return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(msg: dict) -> None:
        received.append(msg)
        if msg.get("type") != "http.response.body":
            return
        body = msg.get("body", b"").decode("utf-8", errors="replace")
        # Each non-comment "\n\n"-terminated chunk is one SSE frame.
        for frame in body.split("\n\n"):
            if frame.strip() and not frame.startswith(":"):
                frame_count[0] += 1
        if frame_count[0] >= expected_frames:
            disconnect_sent.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", 80),
    }
    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=timeout)
    except asyncio.TimeoutError:
        pass

    joined = "".join(
        ev.get("body", b"").decode("utf-8", errors="replace")
        for ev in received
        if ev["type"] == "http.response.body"
    )
    events: list[dict] = []
    for frame in joined.split("\n\n"):
        if not frame.strip() or frame.startswith(":"):
            continue
        data_line = next(
            (ln for ln in frame.splitlines() if ln.startswith("data: ")), None
        )
        if data_line is not None:
            events.append(json.loads(data_line[len("data: "):]))
    return events


def _card_action(event: dict) -> str | None:
    """Mirror of the frontend ``cardActionFor`` in reducer.ts — so the
    Python-side assertion matches what the TS reducer would do.

    Returns:
      - ``"append"``         — new Card added to cards[]
      - ``"upgrade-tool-result"`` — existing tool-call card carries result
      - ``None``             — no card impact
    """
    t = event.get("type")
    if t == EVENT_USER_INPUT:
        return "append"
    if t == "user_interrupt":
        payload = event.get("payload") or {}
        text = payload.get("text")
        return "append" if isinstance(text, str) and text else None
    if t in (EVENT_AGENT_TEXT, EVENT_AGENT_THINKING, EVENT_AGENT_TOOL_CALL):
        return "append"
    if t == EVENT_AGENT_TOOL_RESULT:
        return "upgrade-tool-result"
    if t in ("system_notice", "error"):
        return "append"
    return None


def _reduce_cards(events: list[dict]) -> list[dict]:
    """Pure reducer replayed server-side. Returns the ordered Card list
    a frontend would render for the given event stream.

    Each card is represented as ``{"kind": <type>, "id": <event id>,
    "result_id": <id of paired agent_tool_result when present>}``. This
    skeleton is enough to detect reordering / missing pairings, which is
    exactly the alignment property we care about.
    """
    cards: list[dict] = []
    by_tool_use_id: dict[str, dict] = {}
    for ev in events:
        action = _card_action(ev)
        if action == "append":
            card = {"kind": ev["type"], "id": ev["id"]}
            cards.append(card)
            if ev["type"] == EVENT_AGENT_TOOL_CALL:
                tuid = (ev.get("payload") or {}).get("tool_use_id")
                if isinstance(tuid, str):
                    by_tool_use_id[tuid] = card
        elif action == "upgrade-tool-result":
            tuid = (ev.get("payload") or {}).get("tool_use_id")
            if isinstance(tuid, str) and tuid in by_tool_use_id:
                by_tool_use_id[tuid]["result_id"] = ev["id"]
            else:
                cards.append({"kind": ev["type"], "id": ev["id"]})
    return cards


# ── The money test ──────────────────────────────────────────────────────

class AlignmentTest(unittest.TestCase):
    def test_live_sse_equals_history_replay_and_produces_same_cards(self) -> None:
        """Given a canonical event sequence, the SSE stream, the history
        endpoint, and a reducer fed from either must all converge on the
        same Card sequence — invariant I7."""
        with TemporaryDirectory() as td:
            root = _setup_session(Path(td))
            session_id = "e2e"
            system_dir = root / "_sessions" / session_id

            # Canonical event sequence — one of each for_llm event type
            # in realistic interleaving, plus one system event.
            append_event(system_dir, EVENT_USER_INPUT, {
                "text": "what's the date",
                "source": "cli",
                "caller": None,
                "display_name": None,
            })
            append_event(system_dir, EVENT_AGENT_THINKING, {
                "text": "check via bash",
                "signature": None,
                "summary": None,
                "redacted": False,
                "interrupted": False,
                "reasoning_tokens": 5,
                "duration_ms": 200.0,
            })
            append_event(system_dir, EVENT_AGENT_TEXT, {
                "text": "let me check",
                "model": "test-model",
            })
            append_event(system_dir, EVENT_AGENT_TOOL_CALL, {
                "tool_use_id": "tuse_1",
                "tool_name": "bash",
                "args": {"command": "date"},
            })
            append_event(system_dir, EVENT_AGENT_TOOL_RESULT, {
                "tool_use_id": "tuse_1",
                "tool_name": "bash",
                "result": "Thu Apr 23 2026",
                "is_error": False,
                "is_background": False,
                "duration_ms": 42.0,
            })
            append_event(system_dir, EVENT_AGENT_TEXT, {
                "text": "it's April 23 2026",
                "model": "test-model",
            })

            app = create_app(root / "sessions", root / "_sessions")

            # ── (a) SSE stream: first 6 real frames ──────────────────
            # Patch the keep-alive to a small value so the generator
            # returns after one idle cycle, giving the scope-driven ASGI
            # collector a deterministic end.
            with patch("ui.web.app._SSE_KEEPALIVE_SECONDS", 0.2):
                sse_events = asyncio.run(_collect_sse_events(
                    app,
                    f"/api/sessions/{session_id}/events/stream?cursor=0",
                    expected_frames=6,
                ))

            # ── (b) history replay ────────────────────────────────────
            with TestClient(app) as client:
                resp = client.get(f"/api/sessions/{session_id}/history")
            self.assertEqual(resp.status_code, 200)
            history_events = resp.json()["events"]

            # ── (c) direct IO read for cross-check ───────────────────
            io_events = [e.to_dict() for e in io.read_display_history(session_id)]

            # All three lists must have the same ids + types + payloads.
            self.assertEqual(
                [e["id"] for e in sse_events[:6]],
                [e["id"] for e in history_events],
                "SSE frame ids must match history ids in order",
            )
            self.assertEqual(
                [e["type"] for e in sse_events[:6]],
                [e["type"] for e in history_events],
                "SSE event types must match history types in order",
            )
            self.assertEqual(history_events, io_events)
            for live, replay in zip(sse_events[:6], history_events):
                # Every event read-back is an exact payload dict from
                # events_v1.jsonl — bytewise identical between the two
                # channels.
                self.assertEqual(live, replay)

            # ── (d) the Card sequence — live vs replay reducer ───────
            cards_live = _reduce_cards(sse_events[:6])
            cards_replay = _reduce_cards(history_events)
            self.assertEqual(cards_live, cards_replay)

            # Spot-check the topology: 4 appends (user + thinking + text
            # + tool_call + text = 5 appends, minus 1 because the result
            # upgrades the tool_call in place) → 5 cards.
            kinds = [c["kind"] for c in cards_replay]
            self.assertEqual(kinds, [
                EVENT_USER_INPUT,
                EVENT_AGENT_THINKING,
                EVENT_AGENT_TEXT,
                EVENT_AGENT_TOOL_CALL,
                EVENT_AGENT_TEXT,
            ])
            # The tool_call card picked up its result via the pairing rule.
            tool_card = next(c for c in cards_replay if c["kind"] == EVENT_AGENT_TOOL_CALL)
            self.assertIn("result_id", tool_card)

    def test_io_send_message_round_trips_through_history_and_llm_context(self) -> None:
        """``io.send_message`` writes a user_input event that is readable
        via (a) read_events, (b) read_display_history, and (c)
        build_llm_context — in other words, the writer surface (Phase 5)
        produces events that every reader surface sees identically."""
        with TemporaryDirectory() as td:
            root = _setup_session(Path(td))
            session_id = "e2e"

            with patch("butterfly.runtime.io._SESSIONS_DIR", root / "sessions"), \
                 patch("butterfly.runtime.io._SYSTEM_SESSIONS_DIR", root / "_sessions"):
                # Bypass the service-layer messages_service (which wants
                # a live daemon to nudge) by catching the expected
                # best-effort swallow inside send_message.
                ev = io.send_message(session_id, "hello agent", source="cli")
                self.assertEqual(ev.type, EVENT_USER_INPUT)

                # read_events sees it.
                all_events = list(io.read_events(session_id))
                self.assertEqual(len(all_events), 1)
                self.assertEqual(all_events[0].payload["text"], "hello agent")

                # read_display_history sees it (for_llm=True events
                # always land in display history).
                display = io.read_display_history(session_id)
                self.assertEqual(len(display), 1)
                self.assertEqual(display[0].id, 1)

                # read_llm_context yields the corresponding user Message.
                msgs = io.read_llm_context(session_id)
                self.assertEqual(len(msgs), 1)
                self.assertEqual(msgs[0].role, "user")


if __name__ == "__main__":
    unittest.main()
