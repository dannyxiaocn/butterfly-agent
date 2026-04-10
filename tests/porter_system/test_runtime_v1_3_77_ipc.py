from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from nutshell.runtime.ipc import FileIPC, _context_event_to_display


class IPCUnitTests(unittest.TestCase):
    def test_history_replay_emits_heartbeat_and_tool_calls(self) -> None:
        event = {
            "type": "turn",
            "triggered_by": "heartbeat",
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
        self.assertEqual(display[0]["type"], "heartbeat_trigger")
        self.assertEqual(display[1]["type"], "tool")
        self.assertEqual(display[2]["type"], "agent")

    def test_live_stream_skips_pre_streamed_items(self) -> None:
        event = {
            "type": "turn",
            "triggered_by": "heartbeat",
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
                "triggered_by": "heartbeat",
            }],
        )

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
