"""Regression tests for the Feishu bridge dedup + /new failure-reply
contract — companion to ``test_weixin_dedup.py`` so both bridges have the
same coverage shape (the failure modes are isomorphic: WS redelivery vs.
ilink redelivery; opaque ``/new`` failure replies under the WeChat path
were the field bug; the Feishu equivalent is just as silently easy to
reintroduce).

Each test below maps to one specific contract:

* ``_msg_fingerprint`` keying on ``message_id`` (Feishu's own globally-
  unique key) when present, with a sender + text + bucket fallback;
* ``_seen_or_record`` deque + set staying in sync past the eviction
  window — same bounded-ring invariant as the WeChat bridge;
* ``/new`` exception-path surfacing ``ExcType: msg`` so the next
  regression is debuggable on the Feishu side instead of opaque;
* ``session_init`` imported eagerly at module load — symmetric with the
  WeChat bridge (a broken install must trip on ``import ui.web.feishu``,
  not later when a Feishu user happens to type ``/new``).
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ui.web.feishu import _DEDUPE_WINDOW, _FEISHU_REPLY_FALLBACK_CODES, FeishuBridge


def _bridge(tmp: str) -> FeishuBridge:
    root = Path(tmp)
    return FeishuBridge(root / "sessions", root / "_sessions")


class MsgFingerprintTest(unittest.TestCase):
    """``_msg_fingerprint`` must collapse WS redeliveries (same Feishu
    ``message_id`` arriving twice within a reconnect window) and only
    those — different ``message_id`` values from genuinely different
    messages must hash differently or the bridge would silently drop
    half the user's traffic.
    """

    def test_message_id_is_the_primary_key(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            fp = bridge._msg_fingerprint({"message_id": "om_xxx_1"})
            self.assertEqual(fp, "om_xxx_1")

    def test_redelivery_yields_same_fingerprint(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            fp1 = bridge._msg_fingerprint({"message_id": "om_xxx_1"})
            fp2 = bridge._msg_fingerprint({"message_id": "om_xxx_1"})
            self.assertEqual(fp1, fp2)

    def test_different_messages_do_not_collide(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            fp_a = bridge._msg_fingerprint({"message_id": "om_a"})
            fp_b = bridge._msg_fingerprint({"message_id": "om_b"})
            self.assertNotEqual(fp_a, fp_b)

    def test_fallback_used_when_message_id_missing(self) -> None:
        # A malformed event with no message_id must still produce a
        # stable hash; the fallback is sender + text + 5s bucket.
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            with patch("ui.web.feishu.time.time", return_value=1_700_000_000.0):
                fp1 = bridge._msg_fingerprint({"sender_open_id": "ou_a", "text": "hi"})
                fp2 = bridge._msg_fingerprint({"sender_open_id": "ou_a", "text": "hi"})
            self.assertEqual(fp1, fp2)
            self.assertEqual(len(fp1), 40)  # sha1 hexdigest

    def test_fallback_time_bucket_separates_genuine_repeat(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            with patch("ui.web.feishu.time.time", return_value=1_700_000_000.0):
                fp_early = bridge._msg_fingerprint({"sender_open_id": "ou_a", "text": "ok"})
            with patch("ui.web.feishu.time.time", return_value=1_700_000_006.0):
                fp_late = bridge._msg_fingerprint({"sender_open_id": "ou_a", "text": "ok"})
            self.assertNotEqual(fp_early, fp_late)

    def test_handles_empty_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            with patch("ui.web.feishu.time.time", return_value=1_700_000_000.0):
                fp = bridge._msg_fingerprint({})
            self.assertEqual(len(fp), 40)


class SeenOrRecordTest(unittest.TestCase):
    """The bounded-ring invariant — same shape as the WeChat bridge.
    A regression here would either silently drop legitimate traffic
    (set retains evicted ids) or grow unbounded (set never resyncs).
    """

    def test_first_sighting_returns_false(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            self.assertFalse(bridge._seen_or_record("fp-novel"))

    def test_immediate_redelivery_returns_true(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            bridge._seen_or_record("fp-x")
            self.assertTrue(bridge._seen_or_record("fp-x"))

    def test_distinct_fingerprints_are_independent(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            self.assertFalse(bridge._seen_or_record("fp-1"))
            self.assertFalse(bridge._seen_or_record("fp-2"))
            self.assertTrue(bridge._seen_or_record("fp-1"))
            self.assertTrue(bridge._seen_or_record("fp-2"))

    def test_set_does_not_grow_unbounded_past_window(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            for i in range(_DEDUPE_WINDOW + 32):
                bridge._seen_or_record(f"fp-{i}")
            self.assertLessEqual(len(bridge._seen_set), _DEDUPE_WINDOW)
            self.assertEqual(len(bridge._seen_set), len(bridge._seen_msgs))

    def test_evicted_fingerprint_is_treated_as_novel_again(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            bridge._seen_or_record("fp-old")
            for i in range(_DEDUPE_WINDOW + 1):
                bridge._seen_or_record(f"filler-{i}")
            self.assertNotIn("fp-old", bridge._seen_set)
            self.assertFalse(bridge._seen_or_record("fp-old"))


class ContentParsingTest(unittest.TestCase):
    """``_parse_message_content`` must extract user-visible text from
    the two msg_types we route on (``text`` and ``post``) and degrade
    cleanly to "" for everything else — empty text triggers the
    inbound short-circuit so a bad payload can't reach the agent.
    """

    def test_parses_plain_text(self) -> None:
        from ui.web.feishu import _parse_message_content
        text = _parse_message_content("text", '{"text": "hello"}')
        self.assertEqual(text, "hello")

    def test_parses_post_zh_first_then_en(self) -> None:
        from ui.web.feishu import _parse_message_content
        # zh_cn wins over en_us when both are present.
        payload = (
            '{"zh_cn": {"content": [[{"tag": "text", "text": "你好"}]]},'
            '"en_us": {"content": [[{"tag": "text", "text": "hi"}]]}}'
        )
        self.assertEqual(_parse_message_content("post", payload), "你好")

    def test_falls_back_to_en_when_zh_missing(self) -> None:
        from ui.web.feishu import _parse_message_content
        payload = '{"en_us": {"content": [[{"tag": "text", "text": "hi"}]]}}'
        self.assertEqual(_parse_message_content("post", payload), "hi")

    def test_returns_empty_for_unsupported_types(self) -> None:
        from ui.web.feishu import _parse_message_content
        # image / file / sticker / audio — no text means caller drops.
        self.assertEqual(_parse_message_content("image", '{"image_key": "..."}'), "")
        self.assertEqual(_parse_message_content("file", '{"file_key": "..."}'), "")

    def test_returns_empty_for_malformed_json(self) -> None:
        from ui.web.feishu import _parse_message_content
        self.assertEqual(_parse_message_content("text", "not json"), "")
        self.assertEqual(_parse_message_content("text", ""), "")


class MentionGateTest(unittest.TestCase):
    """``_is_bot_mentioned`` must match on open_id (the authoritative
    Feishu identity) with name as the fallback. The whole point of the
    mention gate is that a group chat doesn't dump every message into
    the agent — getting this wrong silently breaks group operation.
    """

    def test_open_id_match_wins(self) -> None:
        from ui.web.feishu import _is_bot_mentioned
        mentions = [{"key": "1", "open_id": "ou_bot", "name": "Other Bot"}]
        self.assertTrue(_is_bot_mentioned(mentions, "ou_bot", "Real Bot"))

    def test_name_fallback_when_open_id_missing(self) -> None:
        from ui.web.feishu import _is_bot_mentioned
        mentions = [{"key": "1", "open_id": "", "name": "MyBot"}]
        self.assertTrue(_is_bot_mentioned(mentions, "ou_bot", "MyBot"))

    def test_no_mention_returns_false(self) -> None:
        from ui.web.feishu import _is_bot_mentioned
        self.assertFalse(_is_bot_mentioned([], "ou_bot", "MyBot"))

    def test_no_bot_identity_returns_false(self) -> None:
        # Without any configured identity we cannot match — must not
        # accidentally accept "@ everyone" as a bot mention.
        from ui.web.feishu import _is_bot_mentioned
        mentions = [{"key": "1", "open_id": "ou_someone", "name": "Alice"}]
        self.assertFalse(_is_bot_mentioned(mentions, "", ""))


class MentionTokenStripTest(unittest.TestCase):
    """Feishu inlines ``@_user_<key>`` placeholders for each mention.
    Command parsing sees those tokens unless we strip them, so
    ``@bot /new`` becomes ``@_user_1 /new`` and the ``startswith("/")``
    check fails — silent broken-commands regression.
    """

    def test_strips_known_placeholder(self) -> None:
        from ui.web.feishu import _strip_mention_tokens
        text = "@_user_1 /new"
        out = _strip_mention_tokens(text, [{"key": "1", "open_id": "ou_x", "name": ""}])
        self.assertEqual(out, "/new")

    def test_only_strips_listed_keys(self) -> None:
        # An unknown @_user_99 token is preserved — we don't have its
        # mention metadata so guessing would be wrong.
        from ui.web.feishu import _strip_mention_tokens
        text = "@_user_99 hi"
        out = _strip_mention_tokens(text, [{"key": "1", "open_id": "ou_x", "name": ""}])
        self.assertEqual(out, "@_user_99 hi")


class NewCommandFailureReplyTest(unittest.TestCase):
    """The opaque-reply half of the bug — same regression as the WeChat
    bridge. ``/new`` failure must surface ``ExcType: msg`` and print a
    traceback to the web log so the next install-broke regression is
    debuggable instead of silently opaque on the Feishu side.
    """

    def test_new_failure_reply_includes_exception_class(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            fixed = datetime(2026, 5, 12, 11, 59, 36)

            captured: dict[str, str] = {}

            async def _capture_send(chat_id, msg_id, text) -> None:
                captured["text"] = text

            async def _run() -> None:
                with patch(
                    "butterfly.service.sessions_service.create_session",
                    side_effect=ModuleNotFoundError(
                        "No module named 'butterfly.session_engine.session_init'"
                    ),
                ), patch.object(bridge, "_send_reply", new=_capture_send), \
                        patch("ui.web.feishu.datetime") as mock_dt, \
                        patch("ui.web.feishu.uuid.uuid4") as mock_uuid, \
                        patch("ui.web.feishu.traceback.print_exc") as tb_mock:
                    mock_dt.now.return_value = fixed
                    mock_uuid.return_value = SimpleNamespace(hex="abcd1234")
                    await bridge._handle_command(
                        "oc_chat", "p2p", "om_msg_1", "/new",
                    )
                    tb_mock.assert_called_once()

            asyncio.run(_run())

            self.assertIn("创建失败", captured["text"])
            # Class-name prefix is the regression we care about — without
            # it the Feishu reply would be the bare "No module named …" string.
            self.assertIn("ModuleNotFoundError", captured["text"])
            # Original message must still be present so the reply remains
            # informative end-to-end.
            self.assertIn("session_init", captured["text"])
            # Active session must NOT be set for this chat when create
            # failed — same invariant as the WeChat bridge: the next /new
            # mustn't silently reuse a half-built sid.
            self.assertNotIn("oc_chat", bridge._chat_sessions)


class EagerSessionInitImportTest(unittest.TestCase):
    """Symmetric with ``test_weixin_dedup``: ``session_init`` is imported
    at module load so a broken install fails the web server at startup
    rather than after a partial /new succeeds.
    """

    def test_session_init_is_module_level_attribute(self) -> None:
        import ui.web.feishu as feishu_mod
        self.assertTrue(hasattr(feishu_mod, "_session_init"))
        self.assertEqual(
            feishu_mod._session_init.__name__,
            "butterfly.session_engine.session_init",
        )


class PerChatLockTest(unittest.TestCase):
    """``_get_chat_lock`` must hand out one ``asyncio.Lock`` per chat_id
    and reuse it on subsequent calls. The whole point of per-chat
    serialisation (vs hermes' previous global lock) is that a 120s agent
    reply in group X does not freeze group Y — the test pins the *shape*
    that makes that possible (one lock per chat, distinct from each other).
    """

    def test_same_chat_returns_same_lock(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)

            async def _go() -> None:
                a = bridge._get_chat_lock("oc_chat_X")
                b = bridge._get_chat_lock("oc_chat_X")
                self.assertIs(a, b)

            asyncio.run(_go())

    def test_different_chats_get_different_locks(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)

            async def _go() -> None:
                x = bridge._get_chat_lock("oc_chat_X")
                y = bridge._get_chat_lock("oc_chat_Y")
                self.assertIsNot(x, y)

            asyncio.run(_go())

    def test_lock_in_chat_x_does_not_block_chat_y(self) -> None:
        # The end-to-end contract: a long task in chat X must not delay
        # chat Y. This is the regression the per-chat lock fixes vs the
        # previous global lock — without it both chats would serialise.
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)

            async def _go() -> None:
                lock_x = bridge._get_chat_lock("oc_chat_X")
                lock_y = bridge._get_chat_lock("oc_chat_Y")
                acquired_y = asyncio.Event()

                async def _hold_x() -> None:
                    async with lock_x:
                        await acquired_y.wait()  # let Y prove it ran first

                async def _run_y() -> None:
                    async with lock_y:
                        acquired_y.set()

                # Start the long X task, then verify Y completes without
                # waiting for X. asyncio.wait_for tightens "without
                # waiting" to "within 200ms".
                x_task = asyncio.create_task(_hold_x())
                await asyncio.wait_for(_run_y(), timeout=0.2)
                self.assertTrue(acquired_y.is_set())
                x_task.cancel()
                try:
                    await x_task
                except asyncio.CancelledError:
                    pass

            asyncio.run(_go())


class BotIdentityHydrationTest(unittest.TestCase):
    """``_hydrate_bot_identity`` reads bot open_id + name from /bot/v3/info
    so the group @mention gate works without manual env config. The
    contract: on a 0-code response we overwrite the configured values
    with what the API returned; on failure we leave them alone.
    """

    def test_hydration_populates_open_id_and_name(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            # Lark Client.request() is what /bot/v3/info goes through; we
            # never actually invoke it (asyncio.to_thread is mocked) but
            # the attribute must exist so the to_thread arg expression
            # evaluates without an AttributeError.
            bridge._client = SimpleNamespace(request=lambda req: None)

            fake_content = (
                '{"code": 0, "bot": {"open_id": "ou_realbot", '
                '"app_name": "Real Bot"}}'
            )
            fake_resp = SimpleNamespace(raw=SimpleNamespace(content=fake_content))

            async def _run() -> None:
                with patch("ui.web.feishu.BaseRequest") as mock_br, \
                        patch("ui.web.feishu.HttpMethod"), \
                        patch("ui.web.feishu.AccessTokenType"), \
                        patch("ui.web.feishu.asyncio.to_thread",
                              new=AsyncMock(return_value=fake_resp)):
                    # BaseRequest.builder().http_method(...).uri(...).token_types(...).build()
                    mock_br.builder.return_value.http_method.return_value.uri.return_value.token_types.return_value.build.return_value = object()
                    await bridge._hydrate_bot_identity()

            asyncio.run(_run())
            self.assertEqual(bridge._bot_open_id, "ou_realbot")
            self.assertEqual(bridge._bot_name, "Real Bot")

    def test_hydration_failure_preserves_existing_values(self) -> None:
        # If /bot/v3/info fails (network / scope / whatever) we must keep
        # whatever the user provided via env — silently zeroing the
        # identity would break group @mention gating in the wild.
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            bridge._client = SimpleNamespace(request=lambda req: None)
            bridge._bot_open_id = "ou_env"
            bridge._bot_name = "EnvBot"

            async def _run() -> None:
                with patch("ui.web.feishu.BaseRequest") as mock_br, \
                        patch("ui.web.feishu.HttpMethod"), \
                        patch("ui.web.feishu.AccessTokenType"), \
                        patch("ui.web.feishu.asyncio.to_thread",
                              new=AsyncMock(side_effect=RuntimeError("network"))):
                    mock_br.builder.return_value.http_method.return_value.uri.return_value.token_types.return_value.build.return_value = object()
                    await bridge._hydrate_bot_identity()

            asyncio.run(_run())
            self.assertEqual(bridge._bot_open_id, "ou_env")
            self.assertEqual(bridge._bot_name, "EnvBot")
            self.assertIn("hydrate_bot_identity", bridge.error or "")

    def test_hydration_skipped_when_client_not_built(self) -> None:
        # Defensive: no client = no probe (we'd NPE otherwise).
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            self.assertIsNone(bridge._client)

            async def _run() -> None:
                # No mocks needed — must early-return.
                await bridge._hydrate_bot_identity()

            asyncio.run(_run())


class SendRetrySemanticsTest(unittest.TestCase):
    """``_send_reply`` mirrors hermes' ``_feishu_send_with_retry``:

    * Non-success responses are returned, never retried (Feishu's 4xx
      codes are permanent — retry would just burn the user's time).
    * Reply-target-withdrawn codes (230011 / 231003) fall back to
      ``message.create`` exactly once.
    * Python exceptions are retried with 2**attempt backoff.
    """

    def test_constants_match_hermes(self) -> None:
        # Pin the exact set so a typo / drift trips the test.
        self.assertEqual(_FEISHU_REPLY_FALLBACK_CODES, frozenset({230011, 231003}))

    def test_non_success_non_fallback_does_not_create(self) -> None:
        # A generic 4xx (e.g. 240001 permission denied) must NOT trigger
        # the create-fallback. Hermes' contract: only the two specific
        # codes warrant the fallback; everything else stays a failure.
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            bridge._client = object()

            reply_resp = SimpleNamespace(
                code=240001,
                success=lambda: False,
                data=SimpleNamespace(message_id=None),
            )

            create_mock = AsyncMock()

            async def _run() -> None:
                with patch.object(bridge, "_send_reply_once",
                                  new=AsyncMock(return_value=reply_resp)), \
                        patch.object(bridge, "_send_create_once", new=create_mock):
                    await bridge._send_reply("oc_chat", "om_msg", "hi")

            asyncio.run(_run())
            create_mock.assert_not_awaited()

    def test_fallback_code_triggers_create(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            bridge._client = object()

            reply_resp = SimpleNamespace(
                code=230011,
                success=lambda: False,
                data=SimpleNamespace(message_id=None),
            )
            create_resp = SimpleNamespace(
                code=0,
                success=lambda: True,
                data=SimpleNamespace(message_id="om_new"),
            )

            create_mock = AsyncMock(return_value=create_resp)

            async def _run() -> None:
                with patch.object(bridge, "_send_reply_once",
                                  new=AsyncMock(return_value=reply_resp)), \
                        patch.object(bridge, "_send_create_once", new=create_mock):
                    await bridge._send_reply("oc_chat", "om_msg", "hi")

            asyncio.run(_run())
            create_mock.assert_awaited_once()


class StatusEndpointFieldsTest(unittest.TestCase):
    """``/api/feishu/status`` reads private attrs on the bridge directly
    (mirrors the WeChat status endpoint pattern). If those names rename,
    the endpoint silently 500s — pin the surface so renames are loud.
    """

    def test_bridge_exposes_status_endpoint_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge = _bridge(tmp)
            for name in (
                "status", "error", "_current_session", "_app_id",
                "_domain_name", "_bot_open_id", "_bot_name",
            ):
                self.assertTrue(
                    hasattr(bridge, name),
                    f"FeishuBridge missing attr {name!r} read by /api/feishu/status",
                )


if __name__ == "__main__":
    unittest.main()
