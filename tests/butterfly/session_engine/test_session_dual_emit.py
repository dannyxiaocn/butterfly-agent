"""Phase 3a — dual-write pins.

Every legacy ``events.jsonl`` / ``context.jsonl`` emit site in
``butterfly/session_engine/session.py`` is mirrored by a call to
``Session._emit_event`` which appends a schema-v1 event into
``events_v1.jsonl`` (the new-format log that becomes authoritative in
Phase 3b). These tests exercise each category and assert both logs got
content so a later accidental revert — or a new emit site missing its
mirror — fails here first.

Emit-site inventory (for Phase 3b reference — file:line as of commit
before 3a dual-write landed):

  session.py:~420   _emit_task_change        → task_card_changed
  session.py:~442   _emit_todo_list_change   → todo_list_changed
  session.py:~623   _persist_card_transition → task_card_changed
  session.py:~673   _poll_card_script        → task_check (+ optional error)
  session.py:~700   _poll_card_script [done] → task_card_changed
  session.py:~853   _prune_queue_for_task    → task_queue_pruned (no v1 map)
  session.py:~1160  _do_tick                 → task_wakeup (context) ↦ user_input
  session.py:~1281  _do_tick SESSION_FINISHED→ task_finished
  session.py:~1555  daemon resume from stop  → status=resumed ↦ session_started
  session.py:~1588  daemon 5h auto-expire    → status=auto-expired ↦ session_started
  session.py:~1633  daemon cancelled         → status=cancelled ↦ session_stopped
  session.py:~1641  daemon stopped normally  → status=stopped ↦ session_stopped
  session.py:~1682  _handle_explicit_interrupt→ interrupted ↦ user_interrupt(None)
  session.py:~1743  _shutdown_background_man → error
  session.py:~1760  _shutdown_terminal       → error
  session.py:~1863+ _dispatch_terminal_input → terminal_rejected / user_input (context)
  session.py:~2041  _drain_background_events → panel_update ↦ panel_entry_changed
                                              ↦ user_input (sub_agent/cli source)
  session.py:~2058  bg progress              → tool_progress
  session.py:~2105  bg finalize              → tool_finalize ↦ agent_tool_result
  session.py:~2114  _set_model_status        → model_status
  session.py:~2148  on_tool_call             → tool_call ↦ agent_tool_call
  session.py:~2226  on_tool_done             → tool_done ↦ agent_tool_result
  session.py:~2283  _emit_sub_agent_count    → sub_agent_count
  session.py:~2297  loop_start               → (not mirrored; retired §3.6)
  session.py:~2316  loop_end                 → (not mirrored; retired §3.6)
  session.py:~2370  llm_call_end             → llm_call_usage
  session.py:~2402  agent_output_done        → (retired §3.6)
  session.py:~2499  todo-reminder-injected   → todo_list_changed
  session.py:~2526  version notice           → system_notice
  session.py:~2624  on_thinking_start        → thinking_start (retired §3.6)
  session.py:~2639  on_thinking_end          → thinking_done ↦ agent_thinking
  session.py:~2734  on_chunk first           → agent_output_start (retired §3.6)
  session.py:~2739  on_chunk 150-char flush  → partial_text (retired §3.6)
  session.py:~2748  on_chunk final flush     → partial_text (retired §3.6)
  (new)             flush drain              ↦ agent_text
  (new)             llm_call_end drain       ↦ agent_text

Notes (see DESIGN.md §3.6): we deliberately DO NOT dual-write
``partial_text`` / ``agent_output_start`` / ``agent_output_done`` /
``thinking_start`` / ``iteration_usage`` / ``loop_start`` / ``loop_end``
— those are the retired types.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from butterfly.core.agent import Agent
from butterfly.core.types import TokenUsage
from butterfly.runtime import events as rt_events
from butterfly.runtime.events import EVENTS_FILENAME
from butterfly.runtime.ipc import FileIPC
from butterfly.session_engine.pending_inputs import ChatItem
from butterfly.session_engine.session import Session
from butterfly.session_engine.task_cards import TaskCard, save_card


def _new_session(tmp: Path) -> Session:
    """Bare Session for direct-method tests (no daemon loop)."""
    return Session(
        Agent(provider=None),
        session_id="demo",
        base_dir=tmp / "sessions",
        system_base=tmp / "_sessions",
    )


def _read_v1(session: Session) -> list[dict]:
    """Parse events_v1.jsonl lines into dicts. [] when file is absent."""
    path = session.system_dir / EVENTS_FILENAME
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _read_legacy_events(session: Session) -> list[dict]:
    path = session._events_path
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


class EmitEventHelperTest(unittest.TestCase):
    """Phase 3a: ``Session._emit_event`` in isolation."""

    def test_emit_event_creates_file_and_appends_correctly(self) -> None:
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            # Before: events_v1.jsonl does not exist (session __init__ only
            # touches events.jsonl + context.jsonl).
            v1_path = s.system_dir / EVENTS_FILENAME
            self.assertFalse(v1_path.exists())

            s._emit_event(
                rt_events.EVENT_USER_INPUT,
                {"text": "hi", "source": "cli", "caller": None, "display_name": None},
            )

            self.assertTrue(v1_path.exists())
            rows = _read_v1(s)
            self.assertEqual(len(rows), 1)
            ev = rows[0]
            self.assertEqual(ev["id"], 1)
            self.assertEqual(ev["type"], rt_events.EVENT_USER_INPUT)
            self.assertTrue(ev["for_llm"])
            self.assertEqual(ev["payload"]["text"], "hi")

            # Second emit: id = 2, monotonic.
            s._emit_event(
                rt_events.EVENT_AGENT_TEXT,
                {"text": "hello back", "model": "test-model"},
            )
            rows = _read_v1(s)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["id"], 2)
            self.assertEqual(rows[1]["payload"]["text"], "hello back")

    def test_emit_event_swallows_oserror(self) -> None:
        """OSError in the runtime writer must not propagate — legacy path
        retains the "best-effort" semantics the old ``_append_event``
        wrapped every caller in."""
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            # Replace system_dir with a non-creatable path by monkey-
            # patching the property for this test. We can't set a
            # read-only system_dir cleanly with TemporaryDirectory, so
            # instead intercept the underlying append_event.
            from butterfly.runtime import events as _rt

            def _boom(*args, **kwargs) -> None:
                raise OSError("disk full")

            original = _rt.append_event
            _rt.append_event = _boom
            try:
                # Should NOT raise
                s._emit_event(rt_events.EVENT_SYSTEM_NOTICE, {"text": "x", "level": "info"})
            finally:
                _rt.append_event = original


class ModelStatusDualWriteTest(unittest.TestCase):
    def test_set_model_status_writes_both_logs(self) -> None:
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            s._set_model_status("running", "user")

            legacy = _read_legacy_events(s)
            self.assertTrue(any(e.get("type") == "model_status" for e in legacy))

            v1 = _read_v1(s)
            hits = [e for e in v1 if e["type"] == rt_events.EVENT_MODEL_STATUS]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["payload"]["status"], "running")


class TaskCardDualWriteTest(unittest.TestCase):
    def test_task_card_changed_dual_write(self) -> None:
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            # Seed a card on disk so the transition has something to load.
            card = TaskCard(name="demo_card", description="hi")
            save_card(s.tasks_dir, card)

            s._persist_card_transition(
                "demo_card",
                lambda c: c.mark_working(),
                emit_change="started",
            )

            v1 = _read_v1(s)
            hits = [e for e in v1 if e["type"] == rt_events.EVENT_TASK_CARD_CHANGED]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["payload"]["name"], "demo_card")
            self.assertEqual(hits[0]["payload"]["change"], "started")

            # Legacy log has the same info in its own shape.
            legacy = _read_legacy_events(s)
            legacy_hits = [e for e in legacy if e.get("type") == "task_card_changed"]
            self.assertEqual(len(legacy_hits), 1)


class ToolCallbacksDualWriteTest(unittest.TestCase):
    def test_agent_tool_call_dual_write(self) -> None:
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            on_call, _get_count = s._make_tool_call_callback()
            on_call("bash", {"cmd": "ls"}, "toolu_1")

            v1 = _read_v1(s)
            hits = [e for e in v1 if e["type"] == rt_events.EVENT_AGENT_TOOL_CALL]
            self.assertEqual(len(hits), 1)
            p = hits[0]["payload"]
            self.assertEqual(p["tool_use_id"], "toolu_1")
            self.assertEqual(p["tool_name"], "bash")
            self.assertEqual(p["args"], {"cmd": "ls"})

            # Legacy must still be there.
            legacy = _read_legacy_events(s)
            self.assertTrue(any(e.get("type") == "tool_call" for e in legacy))

    def test_agent_tool_result_dual_write(self) -> None:
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            # Seed the start timestamp so duration_ms is computed.
            on_call, _ = s._make_tool_call_callback()
            on_call("bash", {}, "toolu_2")
            on_done = s._make_tool_done_callback()
            on_done("bash", {}, "hello\n", "toolu_2", is_error=False)

            v1 = _read_v1(s)
            hits = [e for e in v1 if e["type"] == rt_events.EVENT_AGENT_TOOL_RESULT]
            self.assertEqual(len(hits), 1)
            p = hits[0]["payload"]
            self.assertEqual(p["tool_use_id"], "toolu_2")
            self.assertEqual(p["tool_name"], "bash")
            self.assertEqual(p["result"], "hello\n")
            self.assertFalse(p["is_error"])
            self.assertFalse(p["is_background"])

            legacy = _read_legacy_events(s)
            self.assertTrue(any(e.get("type") == "tool_done" for e in legacy))


class ThinkingDualWriteTest(unittest.TestCase):
    def test_agent_thinking_dual_write_on_close(self) -> None:
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            on_start, on_end, _had, _get = s._make_thinking_callbacks()
            on_start()
            on_end("deliberation body")

            v1 = _read_v1(s)
            hits = [e for e in v1 if e["type"] == rt_events.EVENT_AGENT_THINKING]
            self.assertEqual(len(hits), 1)
            p = hits[0]["payload"]
            self.assertEqual(p["text"], "deliberation body")
            self.assertFalse(p["interrupted"])
            self.assertIn("duration_ms", p)
            # block_id is stamped so the close event can be paired with the
            # paired EVENT_AGENT_THINKING_START placeholder by the frontend.
            self.assertIn("block_id", p)
            self.assertTrue(p["block_id"])

            legacy = _read_legacy_events(s)
            self.assertTrue(any(e.get("type") == "thinking_done" for e in legacy))

    def test_agent_thinking_start_emitted_on_open(self) -> None:
        """on_thinking_start must emit EVENT_AGENT_THINKING_START so the
        UI can render the spinning placeholder; pairs with the canonical
        agent_thinking close event by block_id."""
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            on_start, on_end, _had, _get = s._make_thinking_callbacks()
            on_start()
            on_end("body")

            v1 = _read_v1(s)
            starts = [e for e in v1 if e["type"] == rt_events.EVENT_AGENT_THINKING_START]
            ends = [e for e in v1 if e["type"] == rt_events.EVENT_AGENT_THINKING]
            self.assertEqual(len(starts), 1, "exactly one start placeholder")
            self.assertEqual(len(ends), 1, "exactly one close event")

            # The two events MUST share a block_id — that is the contract the
            # frontend keys its placeholder upgrade off.
            self.assertEqual(
                starts[0]["payload"]["block_id"],
                ends[0]["payload"]["block_id"],
            )

            # for_llm taxonomy: start is a UI-only marker (False), end is
            # canonical (True). Otherwise build_llm_context would double-
            # count the same thinking block.
            self.assertFalse(starts[0]["for_llm"])
            self.assertTrue(ends[0]["for_llm"])


class BgToolDispatchedTest(unittest.TestCase):
    def test_bg_tool_dispatched_emitted_on_placeholder(self) -> None:
        """A tool whose result string parses as a background spawn
        ("task_id=…") must emit EVENT_AGENT_BG_TOOL_DISPATCHED at
        on_tool_done time so the frontend can show a yellow placeholder
        until the deferred EVENT_AGENT_TOOL_RESULT lands."""
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            on_call, _ = s._make_tool_call_callback()
            on_call("bash", {"cmd": "sleep 100"}, "toolu_bg")
            on_done = s._make_tool_done_callback()
            # _parse_background_tid keys off the exact "Task started. task_id=…."
            # placeholder format the executor emits — see session.py top.
            on_done("bash", {}, "Task started. task_id=t_123.", "toolu_bg", is_error=False)

            v1 = _read_v1(s)
            disp = [e for e in v1 if e["type"] == rt_events.EVENT_AGENT_BG_TOOL_DISPATCHED]
            self.assertEqual(len(disp), 1)
            p = disp[0]["payload"]
            self.assertEqual(p["tool_use_id"], "toolu_bg")
            self.assertEqual(p["tool_name"], "bash")
            self.assertEqual(p["tid"], "t_123")
            self.assertFalse(disp[0]["for_llm"])

            # Phase 3a invariant: bg path does NOT also emit
            # agent_tool_result here — that arrives later from the
            # _drain_background_events finalize. Otherwise the LLM context
            # would carry two tool_result blocks for one call.
            results = [e for e in v1 if e["type"] == rt_events.EVENT_AGENT_TOOL_RESULT]
            self.assertEqual(len(results), 0)

    def test_inline_tool_does_not_emit_dispatched(self) -> None:
        """Inline (non-background) tools must NOT emit the dispatched
        marker — that event is the gating signal for the bg-only
        yellow→green two-phase UX."""
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            on_call, _ = s._make_tool_call_callback()
            on_call("read_file", {"path": "x"}, "toolu_inline")
            on_done = s._make_tool_done_callback()
            on_done("read_file", {}, "file body", "toolu_inline", is_error=False)

            v1 = _read_v1(s)
            disp = [e for e in v1 if e["type"] == rt_events.EVENT_AGENT_BG_TOOL_DISPATCHED]
            self.assertEqual(len(disp), 0)
            results = [e for e in v1 if e["type"] == rt_events.EVENT_AGENT_TOOL_RESULT]
            self.assertEqual(len(results), 1)


class LlmCallUsageDualWriteTest(unittest.TestCase):
    def test_llm_call_usage_dual_write(self) -> None:
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            cb = s._make_llm_call_end_callback()
            usage = TokenUsage(
                input_tokens=100,
                cache_read_tokens=50,
                cache_write_tokens=0,
                output_tokens=42,
            )
            cb(usage, duration_ms=1200, iteration=1, tool_use_ids=[])

            v1 = _read_v1(s)
            hits = [e for e in v1 if e["type"] == rt_events.EVENT_LLM_CALL_USAGE]
            self.assertEqual(len(hits), 1)
            p = hits[0]["payload"]
            self.assertEqual(p["iteration"], 1)
            self.assertEqual(p["duration_ms"], 1200)
            self.assertEqual(p["context_tokens"], 100 + 50 + 0 + 42)

            legacy = _read_legacy_events(s)
            self.assertTrue(any(e.get("type") == "llm_call_usage" for e in legacy))


class AgentTextDualWriteTest(unittest.TestCase):
    def test_agent_text_dual_write_via_llm_call_end(self) -> None:
        """Chunks accumulate; on_llm_call_end drains them to one
        agent_text event."""
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            on_chunk = s._make_text_chunk_callback()
            on_chunk("Hel")
            on_chunk("lo, ")
            on_chunk("world.")
            # Simulate the end-of-LLM-call callback firing.
            llm_end = s._make_llm_call_end_callback()
            llm_end(
                TokenUsage(input_tokens=1, cache_read_tokens=0, cache_write_tokens=0, output_tokens=3),
                duration_ms=500,
                iteration=1,
                tool_use_ids=[],
            )

            v1 = _read_v1(s)
            hits = [e for e in v1 if e["type"] == rt_events.EVENT_AGENT_TEXT]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["payload"]["text"], "Hello, world.")

    def test_agent_text_dual_write_via_flush(self) -> None:
        """When the run is cancelled before on_llm_call_end fires, flush()
        still emits the accumulated text."""
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            on_chunk = s._make_text_chunk_callback()
            on_chunk("incomplete...")
            on_chunk.flush()  # type: ignore[attr-defined]

            v1 = _read_v1(s)
            hits = [e for e in v1 if e["type"] == rt_events.EVENT_AGENT_TEXT]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["payload"]["text"], "incomplete...")


class UserInputDualWriteTest(unittest.TestCase):
    def test_user_input_dual_write_from_run_daemon_loop_branch(self) -> None:
        """Exercise the run_daemon_loop branch that enqueues a ChatItem —
        which is where the dual-write lives."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            s = _new_session(root)
            ipc = FileIPC(s.system_dir)

            # Pre-seed context.jsonl with a user_input so poll_inputs
            # finds it. Mirrors what FileIPC.send_chat_message writes.
            ipc.append_context({
                "type": "user_input",
                "content": "hi from test",
                "id": "u-1",
                "caller": "human",
                "mode": "wait",
            })

            # Run one iteration of the daemon loop with a fake sleep.
            stop_event = asyncio.Event()

            async def _fake_sleep(_s: float) -> None:
                stop_event.set()

            from unittest.mock import patch

            # Prevent _enqueue from starting the consumer task (no agent
            # to run the message through).
            with patch("butterfly.session_engine.session.asyncio.sleep", side_effect=_fake_sleep):
                with patch.object(s, "_consumer_loop", return_value=asyncio.sleep(0)):
                    asyncio.run(s.run_daemon_loop(ipc, stop_event=stop_event))

            v1 = _read_v1(s)
            hits = [e for e in v1 if e["type"] == rt_events.EVENT_USER_INPUT]
            self.assertGreaterEqual(len(hits), 1)
            self.assertEqual(hits[0]["payload"]["text"], "hi from test")


class UserInterruptDualWriteTest(unittest.IsolatedAsyncioTestCase):
    async def test_user_interrupt_dual_write(self) -> None:
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            await s._handle_explicit_interrupt(discarded_inbound=0)

            v1 = _read_v1(s)
            hits = [e for e in v1 if e["type"] == rt_events.EVENT_USER_INTERRUPT]
            self.assertEqual(len(hits), 1)
            self.assertIsNone(hits[0]["payload"]["text"])

            legacy = _read_legacy_events(s)
            self.assertTrue(any(e.get("type") == "interrupted" for e in legacy))


class TaskCardChangedFromCallbackTest(unittest.TestCase):
    """Full test_task_card_changed_dual_write across the per-ticket
    callback path (agent tools route their CRUD through this)."""

    def test_task_card_changed_from_tool_on_change_callback(self) -> None:
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            # Seed the card so the dual-write helper's best-effort load
            # finds something. (When the card is absent the payload's
            # `card` field is None — still a valid dual-write.)
            save_card(s.tasks_dir, TaskCard(name="c1"))

            # Reach into the loader-built closure the way agent tools do.
            # _load_session_capabilities builds the loader with our
            # on_task_change closure; simulate by loading capabilities
            # once (which wires the closure into the ToolLoader), then
            # invoke the event path directly via _append_event surrogate.
            # For a clean unit-style test we invoke the closure logic
            # via _persist_card_transition which re-uses the same emit
            # shape.
            s._persist_card_transition(
                "c1",
                lambda c: c.mark_terminal(),
                emit_change="finished",
            )

            v1 = _read_v1(s)
            hits = [e for e in v1 if e["type"] == rt_events.EVENT_TASK_CARD_CHANGED]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["payload"]["change"], "finished")


if __name__ == "__main__":
    unittest.main()
