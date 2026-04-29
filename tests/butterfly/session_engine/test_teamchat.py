"""Unit tests for the teamchat persistence + helpers."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from butterfly.session_engine.teamchat import (
    TeamChat, cursor_get, cursor_set, format_messages_for_view,
    parse_mentions,
)


class ParseMentionsTests(unittest.TestCase):
    def test_known_member(self) -> None:
        self.assertEqual(
            parse_mentions("@coder please look", ["coder", "planner"]),
            ["coder"],
        )

    def test_case_insensitive(self) -> None:
        self.assertEqual(
            parse_mentions("@CODER and @Planner", ["coder", "planner"]),
            ["coder", "planner"],
        )

    def test_unknown_dropped(self) -> None:
        self.assertEqual(
            parse_mentions("ping @bob and @coder", ["coder"]),
            ["coder"],
        )

    def test_at_all_recognised(self) -> None:
        self.assertEqual(
            parse_mentions("@all heads up", ["coder"]),
            ["all"],
        )

    def test_email_does_not_count(self) -> None:
        self.assertEqual(
            parse_mentions("write to user@example.com about it", ["user"]),
            [],
        )

    def test_dedup_preserves_order(self) -> None:
        self.assertEqual(
            parse_mentions("@coder @planner @coder", ["coder", "planner"]),
            ["coder", "planner"],
        )


class TeamChatPersistenceTests(unittest.TestCase):
    def test_post_assigns_monotonic_seq_and_persists(self) -> None:
        with TemporaryDirectory() as td:
            chat = TeamChat(Path(td), ["a", "b"])
            m1 = chat.post("a", "hello")
            m2 = chat.post("b", "hi")
            self.assertEqual(m1.seq, 1)
            self.assertEqual(m2.seq, 2)
            # Persisted to disk
            lines = (Path(td) / "teamchat.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["sender"], "a")

    def test_messages_since_filters_by_seq(self) -> None:
        with TemporaryDirectory() as td:
            chat = TeamChat(Path(td), ["a", "b"])
            chat.post("a", "first")
            chat.post("b", "second")
            chat.post("a", "third")
            self.assertEqual(
                [m.text for m in chat.messages_since(0)],
                ["first", "second", "third"],
            )
            self.assertEqual(
                [m.text for m in chat.messages_since(2)],
                ["third"],
            )
            self.assertEqual(chat.messages_since(99), [])

    def test_visible_to_excludes_self(self) -> None:
        with TemporaryDirectory() as td:
            chat = TeamChat(Path(td), ["a", "b"])
            m = chat.post("a", "self")
            self.assertFalse(TeamChat.is_visible_to(m, "a"))
            self.assertTrue(TeamChat.is_visible_to(m, "b"))

    def test_addresses_viewer(self) -> None:
        with TemporaryDirectory() as td:
            chat = TeamChat(Path(td), ["a", "b", "c"])
            m_targeted = chat.post("a", "hey @b")
            m_all = chat.post("a", "@all listen up")
            m_none = chat.post("a", "fyi")
            self.assertTrue(TeamChat.addresses_viewer(m_targeted, "b"))
            self.assertFalse(TeamChat.addresses_viewer(m_targeted, "c"))
            self.assertTrue(TeamChat.addresses_viewer(m_all, "c"))
            self.assertFalse(TeamChat.addresses_viewer(m_none, "b"))

    def test_summary_lines_omits_sender_self(self) -> None:
        with TemporaryDirectory() as td:
            chat = TeamChat(Path(td), ["a", "b", "c"])
            chat.post("a", "msg1")
            chat.post("a", "msg2 @b")
            chat.post("c", "msg3")
            chat.post("b", "msg4 from b")  # b is the viewer; should NOT be listed
            unread = chat.messages_since(0)
            summary = TeamChat.summary_lines(unread, viewer="b")
            self.assertIn("a → 2 messages (1 @you)", summary)
            self.assertIn("c → 1 message", summary)
            # b's own posts must not appear in b's summary
            self.assertNotIn(" b → ", summary)
            self.assertIn("Use teamchat_view()", summary)

    def test_summary_empty_when_only_self(self) -> None:
        with TemporaryDirectory() as td:
            chat = TeamChat(Path(td), ["a", "b"])
            chat.post("b", "hi")
            self.assertEqual(
                TeamChat.summary_lines(chat.messages_since(0), viewer="b"),
                "",
            )

    def test_format_messages_empty(self) -> None:
        self.assertEqual(
            format_messages_for_view([]),
            "[teamchat] No unread messages.",
        )


class CursorTests(unittest.TestCase):
    def test_cursor_round_trip(self) -> None:
        with TemporaryDirectory() as td:
            d = Path(td)
            self.assertEqual(cursor_get(d), 0)
            cursor_set(d, 7)
            self.assertEqual(cursor_get(d), 7)
            cursor_set(d, 12)
            self.assertEqual(cursor_get(d), 12)

    def test_cursor_corrupt_falls_back_to_zero(self) -> None:
        with TemporaryDirectory() as td:
            d = Path(td)
            (d / "teamchat_cursor.json").write_text("not json", encoding="utf-8")
            self.assertEqual(cursor_get(d), 0)


if __name__ == "__main__":
    unittest.main()
