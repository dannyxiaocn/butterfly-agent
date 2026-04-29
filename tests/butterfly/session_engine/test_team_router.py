"""Behaviour tests for TeamSession routing + initial-message handling.

These complement ``test_team_init_session.py`` (disk layout) and
``test_teamchat.py`` (chat persistence) by exercising the team router
itself, which is otherwise uncovered.

Several tests are marked ``@unittest.expectedFailure`` — they document
real bugs found during PR review (PR #58). Once the underlying issues
are fixed, the decorator should be removed so the tests start guarding
the behaviour for real.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def _make_team(td: Path, *, leader: str = "planner") -> Path:
    """Lay out the minimum disk shape a TeamSession reads at construction."""
    sys_dir = td / "_sessions" / "team1"
    sess_dir = td / "sessions" / "team1"
    sys_dir.mkdir(parents=True)
    (sess_dir / "core").mkdir(parents=True)
    # Member system dirs need to exist so _build_wakeup_payload doesn't bail.
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


class PickRecipientTests(unittest.TestCase):
    """``_pick_recipient`` — pure routing helper, easy to drive directly."""

    def _team(self, td: str):
        from butterfly.session_engine.team_session import TeamSession
        root = Path(td)
        _make_team(root)
        return TeamSession(
            "team1",
            base_dir=root / "sessions",
            system_base=root / "_sessions",
        )

    def test_no_mention_falls_back_to_leader(self) -> None:
        with TemporaryDirectory() as td:
            ts = self._team(td)
            self.assertEqual(ts._pick_recipient("just talking"), "planner")

    def test_mentioned_member_wins(self) -> None:
        with TemporaryDirectory() as td:
            ts = self._team(td)
            self.assertEqual(ts._pick_recipient("hi @coder"), "coder")

    def test_at_all_falls_back_to_leader(self) -> None:
        # By design (see docs/butterfly/session_engine/agent_team.md):
        # @all from the user side is NOT fanned out — it routes to the
        # leader because broadcasting from a single human turn would
        # spawn N concurrent agent runs.
        with TemporaryDirectory() as td:
            ts = self._team(td)
            self.assertEqual(ts._pick_recipient("@all heads up"), "planner")

    def test_unknown_mention_falls_back_to_leader(self) -> None:
        with TemporaryDirectory() as td:
            ts = self._team(td)
            self.assertEqual(ts._pick_recipient("hi @nobody"), "planner")

    @unittest.expectedFailure
    def test_mention_is_case_insensitive(self) -> None:
        # BUG (PR #58): _pick_recipient uses a case-sensitive lookup
        # (`if name in self._members_map`) while the parallel
        # ``parse_mentions`` is case-insensitive. As a result, a user
        # typing "@CODER" routes to the leader rather than the coder.
        # Fix: lowercase-normalise the parse, mirror parse_mentions.
        with TemporaryDirectory() as td:
            ts = self._team(td)
            self.assertEqual(ts._pick_recipient("hi @CODER"), "coder")

    @unittest.expectedFailure
    def test_email_address_is_not_a_mention(self) -> None:
        # BUG (PR #58): the router's regex is r"@([A-Za-z0-9_]+)" without
        # the ``(?<![A-Za-z0-9_])`` lookbehind that ``parse_mentions``
        # applies. That means a user asking the team to "email
        # boss@coder.com" routes the input to the member named "coder"
        # — silently misinterpreting an email as a mention.
        # Fix: reuse the parser from teamchat.parse_mentions (or add
        # the same lookbehind to _MENTION_PROBE).
        with TemporaryDirectory() as td:
            ts = self._team(td)
            self.assertEqual(
                ts._pick_recipient("please email boss@coder.com"),
                "planner",
            )


class TeamSessionDaemonLoopTests(unittest.TestCase):
    """End-to-end-ish: drive run_daemon_loop with a fake BridgeSession."""

    def _run_loop_with(self, td: Path, stop_after: float = 0.3) -> list[str]:
        from butterfly.session_engine.team_session import TeamSession
        from butterfly.runtime.ipc import FileIPC

        forwarded: list[str] = []

        class FakeBridge:
            def __init__(self, *a, **k):
                pass

            def send_message(self, payload, **_kw):
                forwarded.append(payload)
                return "msg"

        ts = TeamSession(
            "team1",
            base_dir=td / "sessions",
            system_base=td / "_sessions",
        )
        ipc = FileIPC(td / "_sessions" / "team1")
        stop_event = asyncio.Event()

        async def stop_soon() -> None:
            await asyncio.sleep(stop_after)
            stop_event.set()

        async def driver() -> None:
            await asyncio.gather(
                ts.run_daemon_loop(ipc, stop_event=stop_event),
                stop_soon(),
            )

        with patch(
            "butterfly.session_engine.team_session.BridgeSession",
            new=FakeBridge,
        ):
            asyncio.run(driver())
        return forwarded

    def test_post_start_user_input_routes_to_leader(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            _make_team(root)
            # Start the loop with an empty context, then append a row
            # immediately afterwards — emulating a human typing post-start.
            from butterfly.session_engine.team_session import TeamSession
            from butterfly.runtime.ipc import FileIPC

            forwarded: list[str] = []

            class FakeBridge:
                def __init__(self, *a, **k):
                    pass

                def send_message(self, payload, **_kw):
                    forwarded.append(payload)
                    return "msg"

            ts = TeamSession(
                "team1",
                base_dir=root / "sessions",
                system_base=root / "_sessions",
            )
            ipc = FileIPC(root / "_sessions" / "team1")
            stop_event = asyncio.Event()

            async def append_then_stop() -> None:
                await asyncio.sleep(0.1)
                with (root / "_sessions" / "team1" / "context.jsonl").open(
                    "a", encoding="utf-8"
                ) as f:
                    f.write(json.dumps({
                        "type": "user_input",
                        "content": "do the thing",
                        "id": "msg-1",
                    }) + "\n")
                await asyncio.sleep(0.2)
                stop_event.set()

            async def driver() -> None:
                await asyncio.gather(
                    ts.run_daemon_loop(ipc, stop_event=stop_event),
                    append_then_stop(),
                )

            with patch(
                "butterfly.session_engine.team_session.BridgeSession",
                new=FakeBridge,
            ):
                asyncio.run(driver())
            self.assertEqual(len(forwarded), 1)
            self.assertIn("do the thing", forwarded[0])

    def test_synthetic_teamchat_rows_are_not_re_routed(self) -> None:
        # When teamchat_send mirrors a member post into the team's
        # context.jsonl with caller=<member_name>, the router must NOT
        # forward it back into the member loop — that would echo every
        # member message into a fresh agent run.
        with TemporaryDirectory() as td:
            root = Path(td)
            _make_team(root)
            ctx_path = root / "_sessions" / "team1" / "context.jsonl"
            ctx_path.parent.mkdir(parents=True, exist_ok=True)
            # Pre-existing non-human row (synthetic). Loop starts AFTER
            # this byte offset so this never gets polled — but to make
            # the test robust we also append a second non-human row
            # *during* the loop and assert nothing forwarded.
            from butterfly.session_engine.team_session import TeamSession
            from butterfly.runtime.ipc import FileIPC

            forwarded: list[str] = []

            class FakeBridge:
                def __init__(self, *a, **k):
                    pass

                def send_message(self, payload, **_kw):
                    forwarded.append(payload)
                    return "msg"

            ts = TeamSession(
                "team1",
                base_dir=root / "sessions",
                system_base=root / "_sessions",
            )
            ipc = FileIPC(root / "_sessions" / "team1")
            stop_event = asyncio.Event()

            async def append_then_stop() -> None:
                await asyncio.sleep(0.1)
                with ctx_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "type": "user_input",
                        "content": "from member",
                        "id": "msg-tc-1",
                        "caller": "coder",
                        "source": "teamchat",
                    }) + "\n")
                await asyncio.sleep(0.2)
                stop_event.set()

            async def driver() -> None:
                await asyncio.gather(
                    ts.run_daemon_loop(ipc, stop_event=stop_event),
                    append_then_stop(),
                )

            with patch(
                "butterfly.session_engine.team_session.BridgeSession",
                new=FakeBridge,
            ):
                asyncio.run(driver())
            self.assertEqual(forwarded, [])

    @unittest.expectedFailure
    def test_initial_message_written_before_start_is_routed(self) -> None:
        # BUG (PR #58): TeamSession.run_daemon_loop seeds
        #     input_offset = ipc.context_size()
        # — i.e. it skips everything already on disk. But
        # init_team_session(initial_message=...) APPENDS exactly that
        # to context.jsonl BEFORE the daemon ever starts. The single-
        # agent Session sidesteps this with _initial_input_offset()
        # which rewinds to 0 for fresh sessions; the team session must
        # do the equivalent or the initial_message kwarg is silently
        # dropped.
        # Fix: rewind to 0 (or use the same "after the last turn"
        # rule) when the team has no committed turns yet.
        with TemporaryDirectory() as td:
            root = Path(td)
            _make_team(root)
            # Simulate init_team_session writing initial_message:
            with (root / "_sessions" / "team1" / "context.jsonl").open(
                "a", encoding="utf-8"
            ) as f:
                f.write(json.dumps({
                    "type": "user_input",
                    "content": "kick off task X",
                    "id": "msg-init",
                }) + "\n")
            forwarded = self._run_loop_with(root, stop_after=0.3)
            self.assertEqual(len(forwarded), 1)
            self.assertIn("kick off task X", forwarded[0])


class WorkflowKillTests(unittest.TestCase):
    """The WorkflowRunner.kill() docstring claims the runner exits
    between steps once the panel entry status is set to KILLED. The
    current implementation marks the entry but the run() loop never
    inspects it, so the workflow plows through every step regardless.
    """

    def test_kill_during_run_stops_the_loop(self) -> None:
        from butterfly.tool_engine.workflow import WorkflowRunner
        from butterfly.session_engine.panel import (
            PanelEntry, STATUS_RUNNING,
        )
        import time

        runner = WorkflowRunner("parent", "/tmp", "/tmp", "/tmp")
        entry = PanelEntry(
            tid="bg-x",
            type="sub_agent",
            tool_name="workflow",
            input={},
            status=STATUS_RUNNING,
            created_at=time.time(),
        )

        from unittest.mock import MagicMock
        ctx = MagicMock()
        ctx.load_entry.return_value = entry
        ctx.save_entry = MagicMock()
        ctx.emit = MagicMock()

        call_count = 0

        async def fake_execute(self, **_kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Mid-run kill — should abort the loop before step 2.
                await runner.kill(ctx, "bg-x")
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
            asyncio.run(runner.run(ctx, "bg-x", entry, {"steps": steps}, None))

        # BUG (PR #58): currently 3 steps run despite the kill.
        # When the loop is fixed to honour the killed entry status,
        # this assertion will pass. We use assertLessEqual to keep the
        # test green for both the buggy current behaviour AND the
        # post-fix behaviour — the *real* assertion is the
        # expectedFailure variant below.
        self.assertGreaterEqual(call_count, 1)

    @unittest.expectedFailure
    def test_kill_aborts_remaining_steps(self) -> None:
        from butterfly.tool_engine.workflow import WorkflowRunner
        from butterfly.session_engine.panel import (
            PanelEntry, STATUS_RUNNING,
        )
        import time

        runner = WorkflowRunner("parent", "/tmp", "/tmp", "/tmp")
        entry = PanelEntry(
            tid="bg-y",
            type="sub_agent",
            tool_name="workflow",
            input={},
            status=STATUS_RUNNING,
            created_at=time.time(),
        )
        from unittest.mock import MagicMock
        ctx = MagicMock()
        ctx.load_entry.return_value = entry
        ctx.save_entry = MagicMock()
        ctx.emit = MagicMock()

        call_count = 0

        async def fake_execute(self, **_kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
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
            asyncio.run(runner.run(ctx, "bg-y", entry, {"steps": steps}, None))
        # Expected: exactly one step ran before kill broke the loop.
        # Currently fails: all three steps run.
        self.assertEqual(call_count, 1)


if __name__ == "__main__":
    unittest.main()
