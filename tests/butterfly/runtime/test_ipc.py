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
                "id": "turn:2026-01-01T00:00:00",
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
        self.assertEqual(display[2]["id"], "turn:2026-01-01T00:00:00")

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
