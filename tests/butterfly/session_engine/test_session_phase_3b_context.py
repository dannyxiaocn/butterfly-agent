"""Phase 3b pins — events_v1.jsonl as the LLM context source.

Phase 3a (commit a59008b) wired every Session emit site to mirror into
``events_v1.jsonl`` alongside the legacy ``context.jsonl`` /
``events.jsonl`` writes. Phase 3b flips the read path: every tick
rebuilds ``_agent._history`` from events_v1 so the event log is the
single source of truth for LLM context (DESIGN.md §2 I3).

These tests pin:

- ``_rebuild_history_from_events`` produces the same messages as
  ``build_llm_context(read_events(session_dir))``, modulo the
  ``core.types.Message`` coercion.
- ``load_history()`` is now a thin wrapper over the rebuild — it
  populates ``_agent._history`` from events_v1.
- Rebuild is resilient: empty events file → ``_history = []``; cancelled
  run (partial events only) → rebuild reflects whatever events_v1 has.
- The F1-F4 asymmetry fixes hold:
    * F1 — no placeholder EVENT_AGENT_TOOL_RESULT when is_background=True
    * F2 — EVENT_AGENT_TOOL_RESULT / EVENT_TOOL_PROGRESS carry the
      ORIGINATING tool_use_id, not the tid
    * F3 — EVENT_MODEL_STATUS carries the agent's model name
    * F4 — EVENT_AGENT_THINKING emitted with interrupted=True when the
      run is cancelled mid-thought

Does NOT cover end-to-end Agent.run against a real provider — that lives
in ``test_session_dual_emit.py`` for the per-emit assertions.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from butterfly.core.agent import Agent
from butterfly.core.types import Message as CoreMessage
from butterfly.runtime import events as rt_events
from butterfly.runtime.events import EVENTS_FILENAME, read_events
from butterfly.runtime.llm_context import build_llm_context
from butterfly.session_engine.pending_inputs import ChatItem
from butterfly.session_engine.session import Session


def _new_session(tmp: Path) -> Session:
    return Session(
        Agent(provider=None),
        session_id="phase3b",
        base_dir=tmp / "sessions",
        system_base=tmp / "_sessions",
    )


def _v1_path(s: Session) -> Path:
    return s.system_dir / EVENTS_FILENAME


def _read_v1(s: Session) -> list[dict]:
    p = _v1_path(s)
    if not p.exists():
        return []
    out: list[dict] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


class RebuildHistoryFromEventsTest(unittest.TestCase):
    """``_rebuild_history_from_events`` produces correct Messages."""

    def test_rebuild_handles_empty_events_file(self) -> None:
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            # Sanity: events_v1 does not exist yet.
            self.assertFalse(_v1_path(s).exists())

            s._rebuild_history_from_events()
            self.assertEqual(s._agent._history, [])

    def test_rebuild_roundtrips_user_assistant_exchange(self) -> None:
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            s._emit_event(
                rt_events.EVENT_USER_INPUT,
                {"text": "hello", "source": "cli", "caller": None, "display_name": None},
            )
            s._emit_event(
                rt_events.EVENT_AGENT_TEXT,
                {"text": "hi there", "model": "test-model"},
            )
            # A trailing user_input must be trimmed (it's the next tick's
            # pending input; Agent.run will re-append it via the ``input``
            # arg).
            s._emit_event(
                rt_events.EVENT_USER_INPUT,
                {"text": "follow up", "source": "cli", "caller": None, "display_name": None},
            )

            s._rebuild_history_from_events()
            self.assertEqual(len(s._agent._history), 2)
            self.assertEqual(s._agent._history[0].role, "user")
            self.assertEqual(s._agent._history[0].content, "hello")
            self.assertEqual(s._agent._history[1].role, "assistant")
            self.assertEqual(s._agent._history[1].content, "hi there")

    def test_rebuild_collapses_to_string_for_single_text_block(self) -> None:
        """Single-text-block messages become bare strings so the
        ``_reshape_history`` / turn-writer paths that test
        ``isinstance(content, str)`` keep working."""
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            s._emit_event(
                rt_events.EVENT_USER_INPUT,
                {"text": "one", "source": "cli", "caller": None, "display_name": None},
            )
            s._emit_event(
                rt_events.EVENT_AGENT_TEXT,
                {"text": "assistant reply", "model": "test-model"},
            )
            s._rebuild_history_from_events()
            self.assertIsInstance(s._agent._history[0].content, str)
            self.assertIsInstance(s._agent._history[1].content, str)

    def test_rebuild_uses_list_content_for_multi_block_messages(self) -> None:
        """Multi-block messages (e.g. thinking + text) keep a list[dict]
        shape so provider adapters can serialize each block."""
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            s._emit_event(
                rt_events.EVENT_USER_INPUT,
                {"text": "hi", "source": "cli", "caller": None, "display_name": None},
            )
            # Assistant emits a thinking block then a text block — both
            # for_llm=True, both role=assistant — so they group together.
            s._emit_event(
                rt_events.EVENT_AGENT_THINKING,
                {
                    "text": "let me think…",
                    "signature": None,
                    "summary": None,
                    "redacted": False,
                    "interrupted": False,
                    "reasoning_tokens": None,
                    "duration_ms": 500,
                },
            )
            s._emit_event(
                rt_events.EVENT_AGENT_TEXT,
                {"text": "done", "model": "test-model"},
            )
            s._rebuild_history_from_events()
            self.assertEqual(len(s._agent._history), 2)
            asst = s._agent._history[1]
            self.assertEqual(asst.role, "assistant")
            self.assertIsInstance(asst.content, list)
            self.assertEqual(len(asst.content), 2)
            self.assertEqual(asst.content[0]["type"], "thinking")
            self.assertEqual(asst.content[1]["type"], "text")

    def test_rebuild_remaps_tool_result_role_to_tool(self) -> None:
        """tool_result blocks must land on role="tool" in Agent._history.

        ``build_llm_context`` groups EVENT_AGENT_TOOL_RESULT under role="user"
        (DESIGN.md §4 — matches Anthropic's wire shape), but Agent's internal
        convention is ``Message(role="tool", content=tool_results)``
        (core/agent.py:380). The OpenAI Responses provider's
        ``_convert_messages`` dispatches by role: "tool" routes through
        ``_convert_tool_result`` → function_call_output; "user" through
        ``_convert_user`` which only handles "text" blocks and SILENTLY
        DROPS tool_result. That mismatch was the live bug where every user
        message after a todo-list-edit turn hit a 400 BadRequestError
        (function_call without matching function_call_output).
        """
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            # user → assistant(tool_use) → tool(tool_result) chain.
            s._emit_event(
                rt_events.EVENT_USER_INPUT,
                {"text": "do it", "source": "cli", "caller": None, "display_name": None},
            )
            s._emit_event(
                rt_events.EVENT_AGENT_TOOL_CALL,
                {"tool_use_id": "tu1", "tool_name": "bash", "args": {"cmd": "ls"}},
            )
            s._emit_event(
                rt_events.EVENT_AGENT_TOOL_RESULT,
                {
                    "tool_use_id": "tu1",
                    "tool_name": "bash",
                    "result": "file1\nfile2",
                    "is_error": False,
                    "is_background": False,
                    "duration_ms": 12.0,
                },
            )

            s._rebuild_history_from_events()
            self.assertEqual(len(s._agent._history), 3)
            user_msg, asst_msg, tool_msg = s._agent._history
            self.assertEqual(user_msg.role, "user")
            self.assertEqual(asst_msg.role, "assistant")
            # The crucial assertion: rebuild promotes the user-role
            # tool_result message to role="tool" so the OpenAI Responses
            # adapter's `_convert_tool_result` dispatcher fires.
            self.assertEqual(tool_msg.role, "tool")
            self.assertIsInstance(tool_msg.content, list)
            self.assertEqual(tool_msg.content[0]["type"], "tool_result")
            self.assertEqual(tool_msg.content[0]["tool_use_id"], "tu1")

    def test_rebuild_mixed_user_text_and_tool_result_does_not_double_count(self) -> None:
        """A merged user-role message carrying BOTH a TextBlock and a
        ToolResultBlock (produced when a bg agent_tool_result lands AFTER
        a user_input on disk) is split at rebuild: tool_result blocks
        keep the wire shape so the paired function_call survives, text
        blocks get their own user-role Message which the trim guard then
        strips because the dispatcher's ChatItem already owns that text.

        Without this split, on Anthropic the user text appeared twice in
        the LLM context (once in history via the mixed message, once via
        Agent.run's prepended input); on OpenAI ``_convert_tool_result``
        accidentally dropped it. The split + trim path makes it appear
        exactly once via the input regardless of provider.
        """
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            # First user turn — handled by a previous tick.
            s._emit_event(
                rt_events.EVENT_USER_INPUT,
                {"text": "run X", "source": "cli", "caller": None, "display_name": None},
            )
            # Agent dispatches a bg tool.
            s._emit_event(
                rt_events.EVENT_AGENT_TOOL_CALL,
                {"tool_use_id": "tu_bg", "tool_name": "bash", "args": {"cmd": "sleep 30"}},
            )
            # User chats while bg is running — this becomes the next ChatItem.
            s._emit_event(
                rt_events.EVENT_USER_INPUT,
                {"text": "any progress?", "source": "cli", "caller": None, "display_name": None},
            )
            # Bg result lands AFTER the new user_input.
            s._emit_event(
                rt_events.EVENT_AGENT_TOOL_RESULT,
                {
                    "tool_use_id": "tu_bg",
                    "tool_name": "bash",
                    "result": "ok",
                    "is_error": False,
                    "is_background": True,
                    "duration_ms": 30000.0,
                },
            )

            s._rebuild_history_from_events()
            history = s._agent._history

            # The trailing message should carry the tool_result so the
            # paired function_call is preserved …
            tail = history[-1]
            blocks = tail.content if isinstance(tail.content, list) else []
            has_tool_result = any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks
            )
            self.assertTrue(has_tool_result, "tool_result must survive rebuild")

            # … but NOT the text "any progress?", because the dispatcher
            # already extracted it as the next ChatItem and Agent.run will
            # prepend it. Currently FAILS: text is preserved → double count.
            has_progress_text = any(
                (isinstance(b, dict) and b.get("type") == "text"
                 and "any progress" in (b.get("text") or ""))
                for b in blocks
            )
            self.assertFalse(
                has_progress_text,
                "text block 'any progress?' should not be in history — "
                "the dispatched ChatItem owns it; keeping it here causes "
                "Anthropic to see the text twice (history + Agent.run input).",
            )

    def test_rebuild_matches_build_llm_context_modulo_coercion(self) -> None:
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            s._emit_event(
                rt_events.EVENT_USER_INPUT,
                {"text": "hello", "source": "cli", "caller": None, "display_name": None},
            )
            s._emit_event(
                rt_events.EVENT_AGENT_TEXT,
                {"text": "hi", "model": "x"},
            )

            s._rebuild_history_from_events()
            built = build_llm_context(read_events(s.system_dir))
            # Same length, same roles; content coerced to str for
            # single-text-block messages.
            self.assertEqual(len(built), len(s._agent._history))
            for bm, coerced in zip(built, s._agent._history):
                self.assertEqual(bm.role, coerced.role)

    def test_rebuild_swallows_exception_keeps_history(self) -> None:
        """A disk hiccup on rebuild must not clobber the in-memory
        history — the daemon falls back to pre-3b semantics for that
        tick."""
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            pre = [CoreMessage(role="user", content="pre-existing")]
            s._agent._history = list(pre)

            # Patch read_events to raise
            import butterfly.runtime.events as _rt
            original = _rt.read_events
            def _boom(*_a, **_kw):
                raise RuntimeError("disk missing")
            _rt.read_events = _boom
            try:
                s._rebuild_history_from_events()
            finally:
                _rt.read_events = original

            # History still holds the pre-rebuild content
            self.assertEqual(len(s._agent._history), 1)
            self.assertEqual(s._agent._history[0].content, "pre-existing")


class LoadHistoryIsRebuildTest(unittest.TestCase):
    """Change D: ``load_history`` is now a pass-through to rebuild."""

    def test_load_history_populates_from_events_v1(self) -> None:
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            s._emit_event(
                rt_events.EVENT_USER_INPUT,
                {"text": "seed input", "source": "cli", "caller": None, "display_name": None},
            )
            s._emit_event(
                rt_events.EVENT_AGENT_TEXT,
                {"text": "seed reply", "model": "m"},
            )

            s.load_history()
            self.assertEqual(len(s._agent._history), 2)
            self.assertEqual(s._agent._history[0].content, "seed input")
            self.assertEqual(s._agent._history[1].content, "seed reply")

    def test_load_history_on_empty_session_is_noop(self) -> None:
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            s.load_history()
            self.assertEqual(s._agent._history, [])


class LlmContextAcrossTicksTest(unittest.IsolatedAsyncioTestCase):
    """The money test: between two ticks, ``_history`` is rebuilt from
    events_v1 — so whatever the agent saw last run is reconstructable
    from disk alone."""

    async def test_llm_context_built_from_events_across_ticks(self) -> None:
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))

            # --- First tick artifacts: one user_input + one agent_text
            # --- (as would be emitted by a real run).
            s._emit_event(
                rt_events.EVENT_USER_INPUT,
                {"text": "first", "source": "cli", "caller": None, "display_name": None},
            )
            s._emit_event(
                rt_events.EVENT_AGENT_TEXT,
                {"text": "first reply", "model": "m"},
            )

            # --- Second tick: simulate _do_chat's rebuild at start.
            # The trailing user_input this turn is ABOUT to dispatch
            # lands in the log before the rebuild runs (matching how the
            # bridge writes user_input before the dispatcher picks it up).
            s._emit_event(
                rt_events.EVENT_USER_INPUT,
                {"text": "second", "source": "cli", "caller": None, "display_name": None},
            )
            s._rebuild_history_from_events()

            # Trailing user_input is trimmed — Agent.run would re-append it
            # via the `input` arg.
            self.assertEqual(len(s._agent._history), 2)
            self.assertEqual(s._agent._history[0].role, "user")
            self.assertEqual(s._agent._history[0].content, "first")
            self.assertEqual(s._agent._history[1].role, "assistant")
            self.assertEqual(s._agent._history[1].content, "first reply")

    async def test_rebuild_survives_cancelled_run(self) -> None:
        """A cancelled run leaves PARTIAL events in the log. The next
        tick's rebuild picks up whatever is there — no drift relative to
        the event stream itself."""
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            # user_input lands first, then one thinking block, then
            # cancel — no final text. The thinking block is emitted with
            # interrupted=True by the F4 fix.
            s._emit_event(
                rt_events.EVENT_USER_INPUT,
                {"text": "cancel me", "source": "cli", "caller": None, "display_name": None},
            )
            s._emit_event(
                rt_events.EVENT_AGENT_THINKING,
                {
                    "text": "partial",
                    "signature": None,
                    "summary": None,
                    "redacted": False,
                    "interrupted": True,
                    "reasoning_tokens": None,
                    "duration_ms": None,
                },
            )

            s._rebuild_history_from_events()
            # The trailing trim only removes trailing USER messages; an
            # unfinished assistant turn remains — providers accept
            # partial assistant turns as prior context (DESIGN.md §4.4).
            self.assertEqual(len(s._agent._history), 2)
            self.assertEqual(s._agent._history[1].role, "assistant")
            # Interrupted thinking preserved as a thinking content block.
            asst_content = s._agent._history[1].content
            if isinstance(asst_content, list):
                self.assertEqual(asst_content[0]["type"], "thinking")
                self.assertEqual(asst_content[0]["text"], "partial")


class AsymmetryFixesTest(unittest.TestCase):
    """F1-F4: asymmetry fixes listed in Phase 3a's commit body."""

    def test_f1_background_tool_done_does_not_emit_placeholder(self) -> None:
        """F1: on_tool_done's placeholder path (is_background=True)
        MUST NOT emit EVENT_AGENT_TOOL_RESULT — the real result arrives
        later via the _drain_background_events path."""
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            on_call, _ = s._make_tool_call_callback()
            on_call("bash", {"cmd": "sleep 5"}, "toolu_bg1")

            on_done = s._make_tool_done_callback()
            on_done(
                "bash",
                {"cmd": "sleep 5"},
                # Exact placeholder format _parse_background_tid detects
                'Task started. task_id=bg_abc123. Output will arrive…',
                "toolu_bg1",
                is_error=False,
            )

            v1 = _read_v1(s)
            tool_results = [e for e in v1 if e["type"] == rt_events.EVENT_AGENT_TOOL_RESULT]
            self.assertEqual(tool_results, [], "bg placeholder must not dual-emit")

            # The tid→tool_use_id map was stashed so later finalize can
            # pair correctly (F2 below).
            self.assertEqual(s._tid_to_tool_use_id.get("bg_abc123"), "toolu_bg1")

    def test_f1_inline_tool_done_still_emits(self) -> None:
        """Inline (non-background) tools keep their placeholder-free
        emit — regression safeguard for the is_background branch."""
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            on_call, _ = s._make_tool_call_callback()
            on_call("read", {"path": "/etc/hosts"}, "toolu_inline")

            on_done = s._make_tool_done_callback()
            on_done(
                "read",
                {"path": "/etc/hosts"},
                "127.0.0.1 localhost\n",
                "toolu_inline",
                is_error=False,
            )

            v1 = _read_v1(s)
            tool_results = [e for e in v1 if e["type"] == rt_events.EVENT_AGENT_TOOL_RESULT]
            self.assertEqual(len(tool_results), 1)
            p = tool_results[0]["payload"]
            self.assertEqual(p["tool_use_id"], "toolu_inline")
            self.assertFalse(p["is_background"])

    def test_f2_tid_to_tool_use_id_map_seeded(self) -> None:
        """F2: on_tool_done places an entry in ``_tid_to_tool_use_id``
        for the background-placeholder case so the later finalize emit
        can pair with the original agent_tool_call event."""
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            on_done = s._make_tool_done_callback()
            on_done(
                "bash",
                {},
                'Task started. task_id=tid42. Output will arrive…',
                "toolu_real",
                is_error=False,
            )
            self.assertEqual(s._tid_to_tool_use_id["tid42"], "toolu_real")

    def test_f3_model_status_carries_model_name(self) -> None:
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            # Inject a concrete model onto the agent so _set_model_status
            # picks it up.
            s._agent.model = "claude-sonnet-test"
            s._set_model_status("running", "user")

            v1 = _read_v1(s)
            hits = [e for e in v1 if e["type"] == rt_events.EVENT_MODEL_STATUS]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["payload"]["model"], "claude-sonnet-test")
            self.assertEqual(hits[0]["payload"]["status"], "running")

    def test_f4_interrupted_thinking_emits_event(self) -> None:
        """F4: ``_emit_interrupted_thinking_blocks`` fires an
        EVENT_AGENT_THINKING with interrupted=True for each
        still-unupgraded placeholder."""
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            # Simulate the placeholder seeded by on_thinking_start.
            placeholder = [{
                "block_id": "th:1",
                "text": "partial thought",
                "ts": "2026-04-24T00:00:00",
                "interrupted": True,
            }]
            s._emit_interrupted_thinking_blocks(placeholder)

            v1 = _read_v1(s)
            hits = [e for e in v1 if e["type"] == rt_events.EVENT_AGENT_THINKING]
            self.assertEqual(len(hits), 1)
            p = hits[0]["payload"]
            self.assertTrue(p["interrupted"])
            self.assertEqual(p["text"], "partial thought")

    def test_f4_upgraded_thinking_is_not_re_emitted(self) -> None:
        """When on_thinking_end upgrades the placeholder (clearing the
        interrupted flag), the cancel-path helper MUST NOT re-emit — the
        close path already fired an EVENT_AGENT_THINKING with
        interrupted=False."""
        with TemporaryDirectory() as tmp:
            s = _new_session(Path(tmp))
            upgraded = [{
                "block_id": "th:1",
                "text": "complete thought",
                "duration_ms": 500,
                # No `interrupted` key — upgraded.
            }]
            s._emit_interrupted_thinking_blocks(upgraded)

            v1 = _read_v1(s)
            hits = [e for e in v1 if e["type"] == rt_events.EVENT_AGENT_THINKING]
            self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
