from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from butterfly.runtime.ipc import FileIPC, _context_event_to_display, _runtime_event_to_display


class IPCUnitTests(unittest.TestCase):
    def test_history_replay_emits_tool_calls_and_agent(self) -> None:
        event = {
            "type": "turn",
            "triggered_by": "task:default",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "1", "name": "bash", "input": {"cmd": "pwd"}},
                        {"type": "text", "text": "done"},
                    ],
                }
            ],
            "ts": "2026-01-01T00:00:00",
        }

        display = _context_event_to_display(event, for_history=True)
        self.assertEqual(display[0]["type"], "tool")
        self.assertEqual(display[1]["type"], "agent")

    def test_live_stream_skips_pre_streamed_items(self) -> None:
        event = {
            "type": "turn",
            "triggered_by": "task:default",
            "pre_triggered": True,
            "has_streaming_tools": True,
            "messages": [{"role": "assistant", "content": "done"}],
            "ts": "2026-01-01T00:00:00",
        }

        display = _context_event_to_display(event, for_history=False)
        self.assertEqual(
            display,
            [{
                "type": "agent",
                "content": "done",
                "ts": "2026-01-01T00:00:00",
                # v2.0.18: agent events are emitted per-text-block with an
                # indexed id (supports interleaved mode — multiple text
                # outputs per turn); a single-output turn gets index 0.
                "id": "turn:2026-01-01T00:00:00:0",
            }],
        )

    def test_history_replay_emits_thinking_from_persisted_blocks(self) -> None:
        """v2.0.17: turns carry a ``thinking_blocks`` field written by the
        session callback. History replay must prefer this over scanning
        message.content, so providers that don't embed thinking in the
        assistant message (codex reasoning items, Anthropic's text-joiner
        drop) still get their cells restored on re-entry."""
        event = {
            "type": "turn",
            "messages": [{"role": "assistant", "content": "done"}],
            "ts": "2026-01-01T00:00:00",
            "thinking_blocks": [
                {"block_id": "th:1:1", "text": "first body", "duration_ms": 1200, "ts": "2026-01-01T00:00:00.500"},
                {"block_id": "th:1:2", "text": "second body", "duration_ms": 800, "ts": "2026-01-01T00:00:01"},
            ],
        }

        display = _context_event_to_display(event, for_history=True)
        thinking_events = [d for d in display if d["type"] == "thinking"]
        self.assertEqual(len(thinking_events), 2)
        self.assertEqual(thinking_events[0]["content"], "first body")
        self.assertEqual(thinking_events[0]["block_id"], "th:1:1")
        self.assertEqual(thinking_events[0]["duration_ms"], 1200)
        self.assertEqual(thinking_events[0]["id"], "thinking:2026-01-01T00:00:00:persisted:0")
        self.assertEqual(thinking_events[1]["content"], "second body")
        self.assertEqual(thinking_events[1]["block_id"], "th:1:2")

    def test_live_stream_skips_persisted_thinking_blocks(self) -> None:
        """Live SSE must not emit thinking from ``thinking_blocks`` — the
        thinking_start/thinking_done events on events.jsonl already
        rendered the cell, re-emitting would double-paint it on the page."""
        event = {
            "type": "turn",
            "messages": [{"role": "assistant", "content": "done"}],
            "ts": "2026-01-01T00:00:00",
            "thinking_blocks": [{"block_id": "th:1:1", "text": "body", "duration_ms": 100}],
        }

        display = _context_event_to_display(event, for_history=False)
        self.assertFalse(any(d["type"] == "thinking" for d in display))

    def test_history_replay_falls_back_to_content_thinking_when_no_blocks(self) -> None:
        """Legacy sessions written before v2.0.17 don't have ``thinking_blocks``
        but may have Anthropic-style ``{"type":"thinking"}`` blocks in
        message.content. Keep that back-compat path alive."""
        event = {
            "type": "turn",
            "messages": [{
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "legacy body"},
                    {"type": "text", "text": "done"},
                ],
            }],
            "ts": "2026-01-01T00:00:00",
        }

        display = _context_event_to_display(event, for_history=True)
        thinking_events = [d for d in display if d["type"] == "thinking"]
        self.assertEqual(len(thinking_events), 1)
        self.assertEqual(thinking_events[0]["content"], "legacy body")

    def test_history_replay_emits_interleaved_text_and_tools_in_order(self) -> None:
        """v2.0.18: interleaved mode — a turn with multiple assistant messages
        where each contains text + tool_use must emit agent events in
        iteration order, not just the last one. Kimi / codex / gpt-5 commonly
        emit think → tool → text → tool → text in a single run, and the
        re-entered transcript must reflect that sequence.
        """
        event = {
            "type": "turn",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me check."},
                        {"type": "tool_use", "id": "1", "name": "bash", "input": {"cmd": "ls"}},
                    ],
                    "ts": "2026-01-01T00:00:00.100",
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "1", "content": "ok"}],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Now step 2."},
                        {"type": "tool_use", "id": "2", "name": "read", "input": {}},
                    ],
                    "ts": "2026-01-01T00:00:00.200",
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "2", "content": "ok"}],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Done — final answer."},
                    ],
                    "ts": "2026-01-01T00:00:00.300",
                },
            ],
            "ts": "2026-01-01T00:00:00",
            "usage": {"total_tokens": 100},
        }

        display = _context_event_to_display(event, for_history=True)
        types = [d["type"] for d in display]
        self.assertEqual(types, ["agent", "tool", "agent", "tool", "agent"])

        agents = [d for d in display if d["type"] == "agent"]
        self.assertEqual(agents[0]["content"], "Let me check.")
        self.assertEqual(agents[1]["content"], "Now step 2.")
        self.assertEqual(agents[2]["content"], "Done — final answer.")

        # Usage attaches to the LAST agent event only — intermediate cells
        # stay clean so the UI renders token stats once per turn.
        self.assertNotIn("usage", agents[0])
        self.assertNotIn("usage", agents[1])
        self.assertIn("usage", agents[2])

    def test_live_stream_interleaved_turn_emits_only_last_agent_event(self) -> None:
        """v2.0.18 fix (session 2026-04-17_21-36-14-f6c3): in live SSE mode,
        only the FINAL text block's agent event is emitted. Intermediate
        text blocks were already rendered live via partial_text streaming
        + the frontend's tool-call-finalize boundary; re-emitting
        turn-derived agent events for them duplicates the cells.
        Tool events are still emitted in full (frontend needs them for
        history-replay parity and in case of reconnect).
        """
        event = {
            "type": "turn",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me check."},
                        {"type": "tool_use", "id": "1", "name": "bash", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "1", "content": "ok"}],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Got it. Final answer: 42."},
                    ],
                },
            ],
            "ts": "2026-01-01T00:00:00",
        }
        display = _context_event_to_display(event, for_history=False)
        agents = [d for d in display if d["type"] == "agent"]
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["content"], "Got it. Final answer: 42.")
        # Tool events survive — frontend needs them to know which tool
        # cell to flip to `▶ running…` on a reconnect.
        tools = [d for d in display if d["type"] == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "bash")

    def test_live_stream_single_text_block_emits_one_agent_event(self) -> None:
        """Single-text-block turn (non-interleaved, the common case): one
        agent event emitted. The filter shouldn't fire for len==1 agent
        events — otherwise turns with a single final text would produce
        zero display cells on live."""
        event = {
            "type": "turn",
            "messages": [{
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
            }],
            "ts": "2026-01-01T00:00:00",
        }
        display = _context_event_to_display(event, for_history=False)
        agents = [d for d in display if d["type"] == "agent"]
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["content"], "done")

    def test_history_replay_pairs_tool_use_with_tool_result(self) -> None:
        """v2.0.19: history replay must surface each tool's returned output
        on the reloaded cell. events.jsonl (live tool_done) isn't replayed,
        so the pairing has to come from the tool_result block that lives on
        the subsequent user message inside the same turn."""
        event = {
            "type": "turn",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tc-1", "name": "bash", "input": {"cmd": "ls"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tc-1", "content": "file1\nfile2\n"},
                    ],
                },
            ],
            "ts": "2026-01-01T00:00:00",
        }
        display = _context_event_to_display(event, for_history=True)
        tool_events = [d for d in display if d["type"] == "tool"]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0]["result"], "file1\nfile2\n")
        self.assertEqual(tool_events[0]["result_len"], len("file1\nfile2\n"))
        self.assertNotIn("result_truncated", tool_events[0])
        self.assertNotIn("is_error", tool_events[0])

    def test_history_replay_tool_result_truncates_over_cap(self) -> None:
        """tool_result content over the 8 KB cap gets trimmed and flagged.
        Keeps the payload small enough that a re-entered session with many
        large tool outputs doesn't blow up the initial history-load size."""
        huge = "X" * 20_000
        event = {
            "type": "turn",
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "tc-1", "name": "bash", "input": {}}],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "tc-1", "content": huge}],
                },
            ],
            "ts": "2026-01-01T00:00:00",
        }
        display = _context_event_to_display(event, for_history=True)
        tool_ev = next(d for d in display if d["type"] == "tool")
        self.assertEqual(len(tool_ev["result"]), 8000)
        self.assertTrue(tool_ev["result_truncated"])

    def test_history_replay_tool_result_flags_is_error(self) -> None:
        """Tool-error results propagate is_error so the UI can style the cell."""
        event = {
            "type": "turn",
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "tc-1", "name": "bash", "input": {}}],
                },
                {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "tc-1",
                        "content": "permission denied",
                        "is_error": True,
                    }],
                },
            ],
            "ts": "2026-01-01T00:00:00",
        }
        display = _context_event_to_display(event, for_history=True)
        tool_ev = next(d for d in display if d["type"] == "tool")
        self.assertTrue(tool_ev["is_error"])
        self.assertEqual(tool_ev["result"], "permission denied")

    def test_live_stream_assigns_distinct_ids_to_multiple_thinking_blocks(self) -> None:
        event = {
            "type": "turn",
            "messages": [{
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "first"},
                    {"type": "thinking", "thinking": "second"},
                    {"type": "text", "text": "done"},
                ],
            }],
            "ts": "2026-01-01T00:00:00",
        }

        display = _context_event_to_display(event, for_history=False)

        self.assertEqual(display[0]["id"], "thinking:2026-01-01T00:00:00:0")
        self.assertEqual(display[1]["id"], "thinking:2026-01-01T00:00:00:1")
        # v2.0.18: agent event id is indexed per-text-block now.
        self.assertEqual(display[2]["id"], "turn:2026-01-01T00:00:00:0")

    def test_last_running_event_offset_replays_current_stream(self) -> None:
        with TemporaryDirectory() as tmp:
            system_dir = Path(tmp) / "_sessions" / "demo"
            system_dir.mkdir(parents=True)
            ipc = FileIPC(system_dir)

            lines = [
                json.dumps({"type": "status", "value": "idle"}) + "\n",
                json.dumps({"type": "model_status", "state": "running"}) + "\n",
                json.dumps({"type": "partial_text", "content": "hello"}) + "\n",
            ]
            ipc.events_path.write_text("".join(lines), encoding="utf-8")

            expected = len(lines[0].encode("utf-8"))
            self.assertEqual(ipc.last_running_event_offset(), expected)

    def test_last_running_event_offset_ignores_completed_turns(self) -> None:
        with TemporaryDirectory() as tmp:
            system_dir = Path(tmp) / "_sessions" / "demo"
            system_dir.mkdir(parents=True)
            ipc = FileIPC(system_dir)

            lines = [
                json.dumps({"type": "model_status", "state": "running"}) + "\n",
                json.dumps({"type": "partial_text", "content": "hello"}) + "\n",
                json.dumps({"type": "model_status", "state": "idle"}) + "\n",
            ]
            ipc.events_path.write_text("".join(lines), encoding="utf-8")

            self.assertEqual(ipc.last_running_event_offset(), ipc.events_size())

    def test_history_replay_pairs_tool_result_onto_tool_event(self) -> None:
        """PR #36 regression: reloaded tool cells must carry the returned
        result, not render forever as '(pending)'. tool_result blocks from
        the follow-up user/tool message are matched by tool_use_id."""
        event = {
            "type": "turn",
            "ts": "2026-04-17T00:00:00",
            "messages": [
                {"role": "user", "content": "go"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "call_1", "name": "bash",
                         "input": {"command": "echo hi"}},
                    ],
                },
                {
                    "role": "tool",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_1",
                         "content": "hi\n", "is_error": False},
                    ],
                },
                {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            ],
        }
        display = _context_event_to_display(event, for_history=True)
        tool_ev = next(e for e in display if e["type"] == "tool")
        self.assertEqual(tool_ev["result"], "hi\n")
        self.assertEqual(tool_ev["result_len"], 3)
        self.assertNotIn("is_error", tool_ev)
        self.assertNotIn("result_truncated", tool_ev)

    def test_history_replay_marks_tool_result_error_and_truncation(self) -> None:
        long_payload = "X" * 9000
        event = {
            "type": "turn",
            "ts": "2026-04-17T00:00:00",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "call_1", "name": "bash",
                         "input": {"command": "boom"}},
                    ],
                },
                {
                    "role": "tool",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_1",
                         "content": long_payload, "is_error": True},
                    ],
                },
            ],
        }
        tool_ev = next(
            e for e in _context_event_to_display(event, for_history=True)
            if e["type"] == "tool"
        )
        self.assertTrue(tool_ev["result_truncated"])
        self.assertTrue(tool_ev["is_error"])
        self.assertEqual(len(tool_ev["result"]), 8000)

    def test_history_replay_interleaves_persisted_thinking_by_position(self) -> None:
        """think → tool → think → tool ordering preserved on reload via
        position markers (codex/gpt-5 reasoning content blocks) rather
        than ts compare — tool_use blocks have no per-block ts, so a
        ts-based approach would flush every thought before any tool."""
        event = {
            "type": "turn",
            "ts": "2026-04-17T00:00:10",
            # Order of thinking_blocks matches stream order; all share
            # ts < turn ts so a naive ts-sort would dump them up front.
            "thinking_blocks": [
                {"block_id": "th_1", "text": "plan A", "ts": "2026-04-17T00:00:01"},
                {"block_id": "th_2", "text": "plan B", "ts": "2026-04-17T00:00:04"},
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "reasoning", "id": "r1", "summary": []},
                        {"type": "tool_use", "id": "call_1", "name": "bash",
                         "input": {"command": "ls"}},
                    ],
                },
                {
                    "role": "tool",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_1", "content": "ok"},
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "reasoning", "id": "r2", "summary": []},
                        {"type": "tool_use", "id": "call_2", "name": "bash",
                         "input": {"command": "pwd"}},
                    ],
                },
                {
                    "role": "tool",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_2", "content": "/"},
                    ],
                },
            ],
        }
        display = _context_event_to_display(event, for_history=True)
        types = [(e["type"], e.get("content") or e.get("input", {}).get("command"))
                 for e in display]
        self.assertEqual(types, [
            ("thinking", "plan A"),
            ("tool", "ls"),
            ("thinking", "plan B"),
            ("tool", "pwd"),
        ])

    def test_history_replay_tail_sweeps_unpaired_thinking(self) -> None:
        """Persisted thinking blocks beyond the number of reasoning /
        thinking position markers land at the tail instead of being dropped."""
        event = {
            "type": "turn",
            "ts": "2026-04-17T00:00:10",
            "thinking_blocks": [
                {"block_id": "th_1", "text": "plan A", "ts": "2026-04-17T00:00:01"},
                {"block_id": "th_2", "text": "trailing", "ts": "2026-04-17T00:00:09"},
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "reasoning", "id": "r1", "summary": []},
                        {"type": "tool_use", "id": "call_1", "name": "bash",
                         "input": {"command": "ls"}},
                    ],
                },
                {
                    "role": "tool",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_1", "content": "ok"},
                    ],
                },
            ],
        }
        display = _context_event_to_display(event, for_history=True)
        types = [e["type"] for e in display]
        # reasoning → tool → (tail-sweep) thinking
        self.assertEqual(types, ["thinking", "tool", "thinking"])
        self.assertEqual(display[-1]["content"], "trailing")

    def test_history_replay_anthropic_inline_thinking_uses_position(self) -> None:
        """Anthropic round-trips thinking as inline content blocks AND
        persists the text — the inline block acts as the position marker."""
        event = {
            "type": "turn",
            "ts": "2026-04-17T00:00:10",
            "thinking_blocks": [
                {"block_id": "th_1", "text": "persisted text", "ts": "2026-04-17T00:00:01"},
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "ignored-in-favor-of-persisted"},
                        {"type": "tool_use", "id": "call_1", "name": "bash",
                         "input": {"command": "ls"}},
                    ],
                },
                {
                    "role": "tool",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_1", "content": "ok"},
                    ],
                },
            ],
        }
        display = _context_event_to_display(event, for_history=True)
        thinking_events = [e for e in display if e["type"] == "thinking"]
        self.assertEqual(len(thinking_events), 1)
        self.assertEqual(thinking_events[0]["content"], "persisted text")
        # Ordering: thinking → tool
        self.assertEqual([e["type"] for e in display], ["thinking", "tool"])

    def test_runtime_hook_events_pass_through(self) -> None:
        self.assertEqual(
            _runtime_event_to_display({"type": "tool_done", "name": "bash", "result_len": 5, "ts": "T"}),
            [{"type": "tool_done", "name": "bash", "result_len": 5, "ts": "T"}],
        )
        self.assertEqual(
            _runtime_event_to_display({"type": "loop_start", "ts": "T"}),
            [{"type": "loop_start", "ts": "T"}],
        )
        self.assertEqual(
            _runtime_event_to_display({"type": "loop_end", "iterations": 2, "ts": "T"}),
            [{"type": "loop_end", "iterations": 2, "ts": "T"}],
        )
