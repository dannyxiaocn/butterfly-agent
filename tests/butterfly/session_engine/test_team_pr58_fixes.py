"""Regression tests for the four bugs flagged in PR #58 review.

These pin the post-fix behaviour so the bugs don't reappear:

  1. ``TeamSession._pick_recipient`` uses case-insensitive lookup +
     reuses ``parse_mentions`` so `@CODER` routes to ``coder`` and
     ``boss@coder.com`` does NOT route to anyone.
  2. (covered alongside #1)
  3. ``TeamSession.run_daemon_loop`` rewinds to byte 0 on fresh team
     sessions so an ``initial_message`` written by
     ``init_team_session`` lands.
  4. ``WorkflowRunner`` re-checks the panel entry each iteration and
     breaks the loop when ``kill()`` flips it terminal.

PR #60's ``test_team_router.py`` carries `@unittest.expectedFailure`
markers documenting the bugs prior to the fix; the equivalent
post-fix behaviour is locked down here.
"""
from __future__ import annotations

import asyncio
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch


def _make_team_disk(td: Path, *, leader: str = "planner") -> Path:
    sys_dir = td / "_sessions" / "team1"
    sess_dir = td / "sessions" / "team1"
    sys_dir.mkdir(parents=True)
    (sess_dir / "core").mkdir(parents=True)
    (td / "_sessions" / "sess-p").mkdir(parents=True)
    (td / "_sessions" / "sess-c").mkdir(parents=True)
    manifest = {
        "session_id": "team1",
        "kind": "team",
        "leader": leader,
        "members": [
            {"name": "planner", "agent": "a", "teamchat_mode": "default"},
            {"name": "coder",   "agent": "b", "teamchat_mode": "silent"},
        ],
        "members_map": {"planner": "sess-p", "coder": "sess-c"},
    }
    (sys_dir / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return td


class PickRecipientFixTests(unittest.TestCase):
    """Bug 1 + 2 — recipient lookup correctness."""

    def _team(self, td):
        from butterfly.session_engine.team_session import TeamSession
        root = Path(td)
        _make_team_disk(root)
        return TeamSession(
            "team1",
            base_dir=root / "sessions",
            system_base=root / "_sessions",
        )

    def test_case_insensitive_mention_routes_to_canonical_member(self):
        with TemporaryDirectory() as td:
            ts = self._team(td)
            self.assertEqual(ts._pick_recipient("hi @CODER"), "coder")
            self.assertEqual(ts._pick_recipient("@PlAnNeR ping"), "planner")

    def test_email_address_is_not_a_mention(self):
        with TemporaryDirectory() as td:
            ts = self._team(td)
            self.assertEqual(
                ts._pick_recipient("please email boss@coder.com"),
                "planner",   # leader fallback
            )
            # Multiple emails — still no false routing
            self.assertEqual(
                ts._pick_recipient("alice@coder.com cc bob@planner.io"),
                "planner",
            )

    def test_real_mention_after_email_still_works(self):
        with TemporaryDirectory() as td:
            ts = self._team(td)
            self.assertEqual(
                ts._pick_recipient("cc boss@coder.com — @coder fyi"),
                "coder",
            )

    def test_at_all_does_not_route(self):
        with TemporaryDirectory() as td:
            ts = self._team(td)
            # @all is recognised but the router intentionally falls back
            # to the leader (broadcasting one human turn into N agent
            # runs is out of scope for v1).
            self.assertEqual(ts._pick_recipient("@all standup"), "planner")


class InitialMessageFixTests(unittest.TestCase):
    """Bug 3 — initial_message written before daemon start is routed."""

    def test_initial_message_routed_on_fresh_team_session(self):
        from butterfly.session_engine.team_session import TeamSession
        from butterfly.runtime.ipc import FileIPC

        forwarded: list[str] = []

        class FakeBridge:
            def __init__(self, *a, **k):
                pass

            def send_message(self, payload, **_kw):
                forwarded.append(payload)
                return "msg"

        with TemporaryDirectory() as td:
            root = Path(td)
            _make_team_disk(root)
            # Emulate init_team_session(initial_message=...): seed
            # context.jsonl BEFORE the daemon starts. events.jsonl is
            # left empty — that's the "fresh session" signal.
            (root / "_sessions" / "team1" / "context.jsonl").write_text(
                json.dumps({
                    "type": "user_input",
                    "content": "kick off task X",
                    "id": "msg-init",
                }) + "\n",
                encoding="utf-8",
            )

            ts = TeamSession(
                "team1",
                base_dir=root / "sessions",
                system_base=root / "_sessions",
            )
            ipc = FileIPC(root / "_sessions" / "team1")
            stop_event = asyncio.Event()

            async def stop_soon():
                await asyncio.sleep(0.2)
                stop_event.set()

            async def driver():
                await asyncio.gather(
                    ts.run_daemon_loop(ipc, stop_event=stop_event),
                    stop_soon(),
                )

            with patch(
                "butterfly.session_engine.team_session.BridgeSession",
                new=FakeBridge,
            ):
                asyncio.run(driver())

            self.assertEqual(len(forwarded), 1)
            self.assertIn("kick off task X", forwarded[0])

    def test_existing_events_skip_seed_replay_on_restart(self):
        # When events.jsonl is already non-empty (a prior daemon ran
        # and wrote at least one status row), the offset must resume
        # at EOF — otherwise restarting the team daemon would re-route
        # every previous user_input.
        from butterfly.session_engine.team_session import TeamSession
        from butterfly.runtime.ipc import FileIPC

        forwarded: list[str] = []

        class FakeBridge:
            def __init__(self, *a, **k):
                pass

            def send_message(self, payload, **_kw):
                forwarded.append(payload)
                return "msg"

        with TemporaryDirectory() as td:
            root = Path(td)
            _make_team_disk(root)
            sys_dir = root / "_sessions" / "team1"
            # Pre-existing user_input that was already routed in an
            # earlier daemon incarnation.
            (sys_dir / "context.jsonl").write_text(
                json.dumps({
                    "type": "user_input",
                    "content": "old input",
                    "id": "old",
                }) + "\n",
                encoding="utf-8",
            )
            # Non-empty events.jsonl — the "had_prior_run" signal.
            (sys_dir / "events.jsonl").write_text(
                json.dumps({"type": "status", "value": "old"}) + "\n",
                encoding="utf-8",
            )

            ts = TeamSession(
                "team1",
                base_dir=root / "sessions",
                system_base=root / "_sessions",
            )
            ipc = FileIPC(sys_dir)
            stop_event = asyncio.Event()

            async def stop_soon():
                await asyncio.sleep(0.2)
                stop_event.set()

            async def driver():
                await asyncio.gather(
                    ts.run_daemon_loop(ipc, stop_event=stop_event),
                    stop_soon(),
                )

            with patch(
                "butterfly.session_engine.team_session.BridgeSession",
                new=FakeBridge,
            ):
                asyncio.run(driver())

            # Old input is NOT re-routed.
            self.assertEqual(forwarded, [])


class WorkflowKillFixTests(unittest.TestCase):
    """Bug 4 — kill() flipping the panel entry must abort the run loop."""

    def test_kill_aborts_remaining_steps(self):
        from butterfly.tool_engine.workflow import WorkflowRunner
        from butterfly.session_engine.panel import (
            PanelEntry, STATUS_RUNNING,
        )

        runner = WorkflowRunner("parent", "/tmp", "/tmp", "/tmp")
        entry = PanelEntry(
            tid="bg-y",
            type="sub_agent",
            tool_name="workflow",
            input={},
            status=STATUS_RUNNING,
            created_at=time.time(),
        )
        ctx = MagicMock()
        ctx.load_entry.return_value = entry
        ctx.save_entry = MagicMock()
        ctx.emit = MagicMock()

        call_count = 0

        async def fake_execute(self, **_kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Mid-step-1 kill — should abort before step 2 fires.
                await runner.kill(ctx, "bg-y")
            return f"reply-{call_count}"

        steps = [
            {"name": "one",   "task": "do A"},
            {"name": "two",   "task": "do B {prev}"},
            {"name": "three", "task": "do C {prev}"},
        ]
        with patch(
            "butterfly.tool_engine.workflow.SubAgentTool.execute",
            new=fake_execute,
        ):
            rc = asyncio.run(
                runner.run(ctx, "bg-y", entry, {"steps": steps}, None)
            )
        self.assertEqual(call_count, 1)
        # Killed runs return None so the BackgroundTaskManager preserves
        # the kill marker on the panel entry instead of overwriting it
        # with a "completed" exit code.
        self.assertIsNone(rc)

    def test_completed_run_returns_zero(self):
        from butterfly.tool_engine.workflow import WorkflowRunner
        from butterfly.session_engine.panel import (
            PanelEntry, STATUS_RUNNING,
        )

        runner = WorkflowRunner("parent", "/tmp", "/tmp", "/tmp")
        entry = PanelEntry(
            tid="bg-z",
            type="sub_agent",
            tool_name="workflow",
            input={},
            status=STATUS_RUNNING,
            created_at=time.time(),
        )
        ctx = MagicMock()
        ctx.load_entry.return_value = entry
        ctx.save_entry = MagicMock()
        ctx.emit = MagicMock()

        async def fake_execute(self, **_kw):
            return "ok"

        steps = [
            {"name": "one", "task": "do A"},
            {"name": "two", "task": "do B {prev}"},
        ]
        with patch(
            "butterfly.tool_engine.workflow.SubAgentTool.execute",
            new=fake_execute,
        ):
            rc = asyncio.run(
                runner.run(ctx, "bg-z", entry, {"steps": steps}, None)
            )
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
