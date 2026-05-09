"""Regression tests for the WeChat bridge dedup + /new failure-reply fixes
(PR #64). The PR added ``WeixinBridge._msg_fingerprint`` /
``_seen_or_record`` and an exception-class prefix on the ``/new`` failure
reply, but shipped without unit tests for that code — the field bug it
fixes (one ``/new`` producing one success + two opaque "创建失败" replies)
is silently easy to reintroduce.

Each test below maps to one of the three fixes:

* fingerprint stability across ilink redeliveries,
* the bounded ring + set staying in sync past the eviction window,
* ``_run`` actually consulting the dedup ring before spawning a task,
* ``/new`` exception path surfacing ``ExcType: msg`` so the next regression
  is debuggable instead of opaque.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ui.web.weixin import _DEDUPE_WINDOW, WeixinBridge


def _text_msg(from_user: str, text: str, *, message_type: int = 1) -> dict:
    return {
        "from_user_id": from_user,
        "message_type": message_type,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


class MsgFingerprintTest(unittest.TestCase):
    """``_msg_fingerprint`` must collapse ilink redeliveries (same user +
    same text within the 5-second bucket) and only those — different
    senders or different text must hash differently or the dedup degrades
    into "drop everyone's messages while user A is talking".
    """

    def setUp(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.bridge = WeixinBridge(root / "sessions", root / "_sessions")

    def test_redelivery_yields_same_fingerprint(self) -> None:
        # Pin time inside _msg_fingerprint so the 5-second bucket cannot
        # straddle a boundary mid-test.
        with patch("ui.web.weixin.time.time", return_value=1_700_000_000.0):
            fp1 = self.bridge._msg_fingerprint(_text_msg("user-A", "/new"))
            fp2 = self.bridge._msg_fingerprint(_text_msg("user-A", "/new"))
        self.assertEqual(fp1, fp2)

    def test_different_users_do_not_collide(self) -> None:
        with patch("ui.web.weixin.time.time", return_value=1_700_000_000.0):
            fp_a = self.bridge._msg_fingerprint(_text_msg("user-A", "hi"))
            fp_b = self.bridge._msg_fingerprint(_text_msg("user-B", "hi"))
        self.assertNotEqual(fp_a, fp_b)

    def test_different_text_does_not_collide(self) -> None:
        with patch("ui.web.weixin.time.time", return_value=1_700_000_000.0):
            fp_first = self.bridge._msg_fingerprint(_text_msg("user-A", "hi"))
            fp_second = self.bridge._msg_fingerprint(_text_msg("user-A", "bye"))
        self.assertNotEqual(fp_first, fp_second)

    def test_time_bucket_separates_genuine_repeat(self) -> None:
        # A user typing the same word twice ~6 seconds apart must not be
        # silently dropped — that's the failure mode the bucket guards.
        with patch("ui.web.weixin.time.time", return_value=1_700_000_000.0):
            fp_early = self.bridge._msg_fingerprint(_text_msg("user-A", "ok"))
        with patch("ui.web.weixin.time.time", return_value=1_700_000_006.0):
            fp_late = self.bridge._msg_fingerprint(_text_msg("user-A", "ok"))
        self.assertNotEqual(fp_early, fp_late)

    def test_handles_msg_with_no_text_items(self) -> None:
        # Image-only / sticker-only msgs must still produce a stable
        # hash without crashing — text just degrades to "".
        msg = {"from_user_id": "user-A", "item_list": [{"type": 2}]}
        with patch("ui.web.weixin.time.time", return_value=1_700_000_000.0):
            fp = self.bridge._msg_fingerprint(msg)
        self.assertEqual(len(fp), 40)  # sha1 hexdigest

    def test_handles_completely_empty_msg(self) -> None:
        # Defensive: malformed payload (no from_user_id, no item_list)
        # must not raise — _run loops over whatever ilink returns.
        with patch("ui.web.weixin.time.time", return_value=1_700_000_000.0):
            fp = self.bridge._msg_fingerprint({})
        self.assertEqual(len(fp), 40)


class SeenOrRecordTest(unittest.TestCase):
    """``_seen_or_record`` is the actual gate — the field bug was that
    nothing tracked redelivery, so once it exists the deque/set must stay
    in sync past the maxlen boundary or stale fingerprints would re-fire.
    """

    def setUp(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.bridge = WeixinBridge(root / "sessions", root / "_sessions")

    def test_first_sighting_returns_false(self) -> None:
        self.assertFalse(self.bridge._seen_or_record("fp-novel"))

    def test_immediate_redelivery_returns_true(self) -> None:
        self.bridge._seen_or_record("fp-x")
        self.assertTrue(self.bridge._seen_or_record("fp-x"))

    def test_distinct_fingerprints_are_independent(self) -> None:
        self.assertFalse(self.bridge._seen_or_record("fp-1"))
        self.assertFalse(self.bridge._seen_or_record("fp-2"))
        self.assertTrue(self.bridge._seen_or_record("fp-1"))
        self.assertTrue(self.bridge._seen_or_record("fp-2"))

    def test_set_does_not_grow_unbounded_past_window(self) -> None:
        # Without the set-rebuild branch, the deque would auto-evict but
        # the set would keep growing — breaking the "bounded ring"
        # invariant the comment promises.
        for i in range(_DEDUPE_WINDOW + 32):
            self.bridge._seen_or_record(f"fp-{i}")
        self.assertLessEqual(len(self.bridge._seen_set), _DEDUPE_WINDOW)
        self.assertEqual(len(self.bridge._seen_set), len(self.bridge._seen_msgs))

    def test_evicted_fingerprint_is_treated_as_novel_again(self) -> None:
        # Once a fingerprint falls out of the ring, the same fp must be
        # accepted again — the dedup is intentionally short-memory so it
        # can't permanently silence a user.
        self.bridge._seen_or_record("fp-old")
        for i in range(_DEDUPE_WINDOW + 1):
            self.bridge._seen_or_record(f"filler-{i}")
        self.assertNotIn("fp-old", self.bridge._seen_set)
        self.assertFalse(self.bridge._seen_or_record("fp-old"))


class RunLoopDedupTest(unittest.TestCase):
    """End-to-end: a single ilink batch carrying the same msg twice must
    spawn ``_process_message`` exactly once — the field symptom (one
    success + two failures) was caused by this not being true.
    """

    def test_run_skips_redelivered_message_within_one_batch(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = WeixinBridge(root / "sessions", root / "_sessions")

            redelivered_batch = {
                "msgs": [
                    _text_msg("user-A", "/new"),
                    _text_msg("user-A", "/new"),  # ilink redelivery
                ],
                "get_updates_buf": "",
            }

            call_count = 0

            async def _fake_process(_client, _msg) -> None:
                nonlocal call_count
                call_count += 1

            poll_calls = 0

            async def _fake_get_updates(_client) -> dict:
                nonlocal poll_calls
                poll_calls += 1
                if poll_calls == 1:
                    return redelivered_batch
                # Yield to the loop so tasks spawned by the first batch
                # actually execute before _run cancels its _pending set.
                await asyncio.sleep(0)
                raise asyncio.CancelledError

            async def _drive() -> None:
                with patch("ui.web.weixin.time.time", return_value=1_700_000_000.0), \
                        patch.object(bridge, "_get_updates", side_effect=_fake_get_updates), \
                        patch.object(bridge, "_save_sync_cursor"), \
                        patch.object(bridge, "_process_message", side_effect=_fake_process):
                    try:
                        await bridge._run()
                    except asyncio.CancelledError:
                        pass

            asyncio.run(_drive())
            self.assertEqual(call_count, 1)

    def test_run_skips_bots_own_message_before_dedup(self) -> None:
        # message_type == 2 must be filtered before the dedup ring even
        # sees it, otherwise a chatty bot would burn its own ring slots.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = WeixinBridge(root / "sessions", root / "_sessions")

            batch = {
                "msgs": [_text_msg("bot", "echo", message_type=2)],
                "get_updates_buf": "",
            }
            poll_calls = 0

            async def _fake_get_updates(_client) -> dict:
                nonlocal poll_calls
                poll_calls += 1
                if poll_calls == 1:
                    return batch
                raise asyncio.CancelledError

            process_mock = AsyncMock()

            async def _drive() -> None:
                with patch.object(bridge, "_get_updates", side_effect=_fake_get_updates), \
                        patch.object(bridge, "_save_sync_cursor"), \
                        patch.object(bridge, "_process_message", new=process_mock):
                    try:
                        await bridge._run()
                    except asyncio.CancelledError:
                        pass

            asyncio.run(_drive())
            process_mock.assert_not_awaited()
            # Crucially the bot's own msg must NOT have consumed a ring slot.
            self.assertEqual(len(bridge._seen_set), 0)


class NewCommandFailureReplyTest(unittest.TestCase):
    """The opaque-reply half of the bug: a failure inside ``/new`` used to
    surface as ``⚠️ 创建失败: <bare exc message>`` with no class name and
    no traceback in the log. The fix prepends ``ExcType:`` and prints the
    traceback. Both are user-visible diagnostics and worth pinning down.
    """

    def test_new_failure_reply_includes_exception_class(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = WeixinBridge(root / "sessions", root / "_sessions")
            fixed = datetime(2026, 5, 8, 11, 59, 36)

            captured: dict[str, str] = {}

            async def _capture_send(_client, _user, text, _ctx) -> None:
                captured["text"] = text

            async def _run() -> None:
                with patch(
                    "butterfly.service.sessions_service.create_session",
                    side_effect=ModuleNotFoundError("No module named 'butterfly.session_engine.session_init'"),
                ), patch.object(bridge, "_send_text", new=_capture_send), \
                        patch("ui.web.weixin.datetime") as mock_dt, \
                        patch("ui.web.weixin.uuid.uuid4") as mock_uuid, \
                        patch("ui.web.weixin.traceback.print_exc") as tb_mock:
                    mock_dt.now.return_value = fixed
                    mock_uuid.return_value = SimpleNamespace(hex="abcd1234")
                    await bridge._handle_command(object(), "user-A", "/new", None)
                    # The traceback is what makes the next regression
                    # debuggable; assert it actually fires.
                    tb_mock.assert_called_once()

            asyncio.run(_run())

            self.assertIn("创建失败", captured["text"])
            # Class-name prefix is the regression we care about — without
            # it the WeChat reply was the bare "No module named …" string.
            self.assertIn("ModuleNotFoundError", captured["text"])
            # Original message must still be present so the reply remains
            # informative end-to-end.
            self.assertIn("session_init", captured["text"])
            # Active session must NOT be set when create failed — the
            # original bug's redelivery loop hinged on this staying None
            # so the next /new wouldn't silently reuse a half-built sid.
            self.assertIsNone(bridge._current_session)


class EagerSessionInitImportTest(unittest.TestCase):
    """The third fix: ``session_init`` is now imported at module load.
    A broken install must trip on ``import ui.web.weixin``, not later when
    a WeChat user happens to type ``/new``. The simplest assertion is
    that the symbol is bound on the module after import.
    """

    def test_session_init_is_module_level_attribute(self) -> None:
        import ui.web.weixin as weixin_mod
        self.assertTrue(hasattr(weixin_mod, "_session_init"))
        # And it really is the session_init module, not e.g. an alias
        # accidentally rebound to something else.
        self.assertEqual(
            weixin_mod._session_init.__name__,
            "butterfly.session_engine.session_init",
        )


if __name__ == "__main__":
    unittest.main()
