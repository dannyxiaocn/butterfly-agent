"""Unit tests for TeamSpec parsing."""
from __future__ import annotations

import unittest

from butterfly.session_engine.team import is_team_manifest, parse_team_spec


class TeamSpecTests(unittest.TestCase):
    def _valid(self) -> dict:
        return {
            "kind": "team",
            "leader": "planner",
            "description": "two-person team",
            "members": [
                {"name": "planner", "agent": "agent", "teamchat_mode": "default"},
                {"name": "coder", "agent": "butterfly_dev", "teamchat_mode": "silent"},
            ],
        }

    def test_is_team_manifest(self) -> None:
        self.assertTrue(is_team_manifest({"kind": "team"}))
        self.assertFalse(is_team_manifest({"kind": "agent"}))
        self.assertFalse(is_team_manifest({}))

    def test_parse_ok(self) -> None:
        spec = parse_team_spec("my_team", self._valid())
        self.assertEqual(spec.leader, "planner")
        self.assertEqual(spec.member_names, ("planner", "coder"))
        self.assertEqual(spec.member("coder").agent, "butterfly_dev")
        self.assertEqual(spec.member("coder").mode, "silent")

    def test_missing_members(self) -> None:
        with self.assertRaisesRegex(ValueError, "members"):
            parse_team_spec("t", {"kind": "team", "leader": "x", "members": []})

    def test_missing_leader(self) -> None:
        m = self._valid()
        m["leader"] = ""
        with self.assertRaisesRegex(ValueError, "leader"):
            parse_team_spec("t", m)

    def test_leader_not_member(self) -> None:
        m = self._valid()
        m["leader"] = "ghost"
        with self.assertRaisesRegex(ValueError, "leader"):
            parse_team_spec("t", m)

    def test_duplicate_member_name(self) -> None:
        m = self._valid()
        m["members"][1]["name"] = "planner"
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_team_spec("t", m)

    def test_invalid_mode(self) -> None:
        m = self._valid()
        m["members"][0]["teamchat_mode"] = "block"
        with self.assertRaisesRegex(ValueError, "teamchat_mode"):
            parse_team_spec("t", m)

    def test_default_mode_when_unspecified(self) -> None:
        m = self._valid()
        del m["members"][0]["teamchat_mode"]
        spec = parse_team_spec("t", m)
        self.assertEqual(spec.member("planner").mode, "default")

    def test_missing_agent_field(self) -> None:
        m = self._valid()
        m["members"][0]["agent"] = ""
        with self.assertRaisesRegex(ValueError, "agent"):
            parse_team_spec("t", m)


if __name__ == "__main__":
    unittest.main()
