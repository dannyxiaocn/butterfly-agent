"""Pin the v2.0.17 thinking-callbacks contract.

PR #33 reshapes ``Session._make_thinking_callbacks`` into a 4-tuple
``(on_thinking_start, on_thinking_end, had_any, get_collected)`` so the
session can persist a ``thinking_blocks`` list on every turn. These
tests lock down the observable behaviour consumed by ``_do_chat`` /
``_do_tick`` / ``_save_partial_chat_turn``:

* ``get_collected()`` returns blocks in completion order with the
  block_id allocated by the matching start.
* ``had_any()`` flips to True only after the first ``on_thinking_end``.
* An interrupted block (start without end) is NOT collected — matches
  the live ``thinking_start``/``thinking_done`` contract and prevents
  history replay from inventing a block that the provider never closed.
* A spurious ``on_thinking_end`` with no pending start is still
  captured (defensive path) so no user-visible text is lost.
* Each ``on_thinking_end`` writes a matching ``thinking_done`` event to
  events.jsonl — the frontend's live path keys off this.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from butterfly.core.agent import Agent
from butterfly.runtime.ipc import FileIPC
from butterfly.session_engine.session import Session


class ThinkingCallbacksTest(unittest.TestCase):
    def _new_session(self, tmp: Path) -> Session:
        return Session(
            Agent(provider=None),
            session_id="demo",
            base_dir=tmp / "sessions",
            system_base=tmp / "_sessions",
        )

    def test_returns_four_callables(self) -> None:
        with TemporaryDirectory() as tmp:
            s = self._new_session(Path(tmp))
            tup = s._make_thinking_callbacks()
            self.assertEqual(len(tup), 4)
            on_start, on_end, had_any, get_collected = tup
            self.assertTrue(callable(on_start))
            self.assertTrue(callable(on_end))
            self.assertTrue(callable(had_any))
            self.assertTrue(callable(get_collected))

    def test_had_any_is_false_before_first_end(self) -> None:
        with TemporaryDirectory() as tmp:
            s = self._new_session(Path(tmp))
            on_start, _, had_any, _ = s._make_thinking_callbacks()
            self.assertFalse(had_any())
            on_start()
            self.assertFalse(had_any())  # start alone doesn't flip

    def test_get_collected_returns_matched_block_ids_in_order(self) -> None:
        with TemporaryDirectory() as tmp:
            s = self._new_session(Path(tmp))
            on_start, on_end, had_any, get_collected = s._make_thinking_callbacks()
            on_start()
            on_end("first body")
            on_start()
            on_end("second body")
            self.assertTrue(had_any())
            blocks = get_collected()
            self.assertEqual(len(blocks), 2)
            self.assertEqual(blocks[0]["text"], "first body")
            self.assertEqual(blocks[1]["text"], "second body")
            # Each block must carry the same block_id the matching
            # thinking_done event wrote to events.jsonl — otherwise the
            # frontend's data-block-id dedup can't pair replayed cells
            # with the live-stream cell.
            ipc = FileIPC(s.system_dir)
            events = [
                json.loads(line) for line in ipc.events_path.read_text().splitlines()
                if line.strip()
            ]
            done_events = [e for e in events if e.get("type") == "thinking_done"]
            self.assertEqual(len(done_events), 2)
            self.assertEqual(blocks[0]["block_id"], done_events[0]["block_id"])
            self.assertEqual(blocks[1]["block_id"], done_events[1]["block_id"])
            for b in blocks:
                self.assertIn("duration_ms", b)
                self.assertIn("ts", b)

    def test_interrupted_block_is_not_collected(self) -> None:
        """A ``thinking_start`` with no matching ``thinking_end`` (mid-turn
        cancel) must NOT surface in ``get_collected()`` — otherwise the
        persisted turn would carry a block the frontend never saw finalize
        and history replay would render a cell without the 'Thought for Xs'
        body."""
        with TemporaryDirectory() as tmp:
            s = self._new_session(Path(tmp))
            on_start, on_end, had_any, get_collected = s._make_thinking_callbacks()
            on_start()
            on_end("completed body")
            on_start()  # never closed — simulates cancel mid-thinking
            blocks = get_collected()
            self.assertEqual(len(blocks), 1)
            self.assertEqual(blocks[0]["text"], "completed body")
            self.assertTrue(had_any())

    def test_orphan_end_without_start_is_still_captured(self) -> None:
        """Defensive path: provider emits thinking_end with no matching
        start. The block must still be captured so the body isn't silently
        dropped, and a synthesised block_id must be written to both the
        event and the collected entry."""
        with TemporaryDirectory() as tmp:
            s = self._new_session(Path(tmp))
            _, on_end, had_any, get_collected = s._make_thinking_callbacks()
            on_end("orphan body")
            blocks = get_collected()
            self.assertEqual(len(blocks), 1)
            self.assertEqual(blocks[0]["text"], "orphan body")
            self.assertTrue(blocks[0]["block_id"].startswith("th:"))
            self.assertTrue(had_any())

    def test_get_collected_returns_snapshot_not_live_reference(self) -> None:
        """``get_collected()`` hands back a ``list(...)`` copy, so callers
        that mutate the list can't corrupt the session's internal buffer.
        Protects ``_save_chat_turn``/``_save_partial_chat_turn`` from
        accidental shared-state bugs."""
        with TemporaryDirectory() as tmp:
            s = self._new_session(Path(tmp))
            on_start, on_end, _, get_collected = s._make_thinking_callbacks()
            on_start()
            on_end("body")
            snapshot = get_collected()
            snapshot.clear()
            # Second call still returns the block — the internal buffer
            # was not affected by the external mutation.
            self.assertEqual(len(get_collected()), 1)


if __name__ == "__main__":
    unittest.main()
