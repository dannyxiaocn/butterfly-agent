from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from butterfly.runtime.bridge import BoundedIDSet, BridgeSession
from butterfly.runtime.env import load_dotenv
from butterfly.runtime.ipc import FileIPC


class RuntimeTest(unittest.TestCase):
    def test_file_ipc_round_trips_context_and_events(self) -> None:
        with TemporaryDirectory() as tmp:
            system_dir = Path(tmp) / "_sessions" / "demo"
            system_dir.mkdir(parents=True)
            ipc = FileIPC(system_dir)
            msg_id = ipc.send_message("hello")
            ipc.append_context(
                {
                    "type": "turn",
                    "user_input_id": msg_id,
                    "messages": [{"role": "assistant", "content": "world"}],
                }
            )
            history = [event for event, _ in ipc.tail_history()]
        self.assertEqual(history[0]["type"], "user")
        self.assertEqual(history[1]["type"], "agent")
        self.assertEqual(history[1]["content"], "world")

    def test_bridge_session_deduplicates_replayed_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            system_dir = Path(tmp) / "_sessions" / "demo"
            system_dir.mkdir(parents=True)
            ipc = FileIPC(system_dir)
            ipc.append_context({"type": "user_input", "id": "same", "content": "hello"})
            bridge = BridgeSession(system_dir)
            first_pass = list(bridge.iter_events())
            second_pass = list(bridge.iter_events())
        self.assertEqual(len(first_pass), 1)
        self.assertEqual(second_pass, [])

    def test_bounded_id_set_evicts_oldest_entry(self) -> None:
        ids = BoundedIDSet(capacity=2)
        ids.add("a")
        ids.add("b")
        ids.add("c")
        self.assertFalse(ids.has("a"))
        self.assertTrue(ids.has("c"))

    def test_load_dotenv_prefers_existing_environment_values(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("KEEP=from-dotenv\nNEW=value\n", encoding="utf-8")
            old_cwd = Path.cwd()
            os.chdir(root)
            try:
                os.environ["KEEP"] = "existing"
                os.environ.pop("NEW", None)
                load_dotenv(root)
                self.assertEqual(os.environ["KEEP"], "existing")
                self.assertEqual(os.environ["NEW"], "value")
            finally:
                os.chdir(old_cwd)
                os.environ.pop("KEEP", None)
                os.environ.pop("NEW", None)

    def test_bridge_wait_for_reply_matches_merged_id(self) -> None:
        import asyncio
        with TemporaryDirectory() as tmp:
            system_dir = Path(tmp) / "_sessions" / "demo"
            system_dir.mkdir(parents=True)
            ipc = FileIPC(system_dir)
            first_id = "id-first"
            second_id = "id-second"
            ipc.append_context({"type": "user_input", "id": first_id, "content": "first"})
            ipc.append_context({"type": "user_input", "id": second_id, "content": "second"})
            bridge = BridgeSession(system_dir)

            async def _inject():
                await asyncio.sleep(0.1)
                ipc.append_context({
                    "type": "turn",
                    "user_input_id": second_id,
                    "merged_user_input_ids": [first_id, second_id],
                    "messages": [{"role": "assistant", "content": "merged reply"}],
                })

            async def _run():
                asyncio.create_task(_inject())
                reply = await bridge.async_wait_for_reply(first_id, timeout=2.0)
                return reply

            reply = asyncio.run(_run())
            self.assertEqual(reply, "merged reply")


if __name__ == "__main__":
    unittest.main()
