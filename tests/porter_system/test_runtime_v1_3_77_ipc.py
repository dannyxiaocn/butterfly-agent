from __future__ import annotations

import unittest

from nutshell.runtime.ipc import _context_event_to_display


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
        self.assertEqual(display, [{"type": "agent", "content": "done", "ts": "2026-01-01T00:00:00", "triggered_by": "heartbeat"}])

