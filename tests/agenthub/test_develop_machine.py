"""Tests for the Develop-Machine workflow team.

Covers the invariants that make the workflow correct *as configured* —
the runtime team-router / teamchat plumbing has its own tests under
``tests/butterfly/session_engine/``. This file only verifies:

  1. The team manifest parses and points at three role agents.
  2. Each role agent loads through ``AgentLoader``.
  3. Teamchat modes encode the communication graph (developer & reviewer
     are silent; checker is default).
  4. Every role agent's ``tools.md`` includes ``teamchat_send`` /
     ``teamchat_view`` (the team-coordination tools).
  5. Role prompts encode the "talk only to checker" rule on the
     developer and reviewer sides.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from butterfly.session_engine.agent_loader import AgentLoader
from butterfly.session_engine.team import load_team_spec


_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_BASE = _REPO_ROOT / "agenthub"
_TEAM_DIR = _AGENT_BASE / "develop_machine"

_ROLE_AGENTS = ("dm_developer", "dm_checker", "dm_reviewer")
_EXPECTED_MEMBERS = (
    ("developer", "dm_developer", "silent"),
    ("checker",   "dm_checker",   "default"),
    ("reviewer",  "dm_reviewer",  "silent"),
)


class DevelopMachineTeamSpecTests(unittest.TestCase):
    def test_team_manifest_parses(self) -> None:
        spec = load_team_spec(_TEAM_DIR)
        self.assertEqual(spec.name, "develop_machine")
        self.assertEqual(spec.leader, "developer")
        self.assertEqual(spec.member_names, ("developer", "checker", "reviewer"))

    def test_member_agent_bindings(self) -> None:
        spec = load_team_spec(_TEAM_DIR)
        for name, agent, mode in _EXPECTED_MEMBERS:
            with self.subTest(member=name):
                m = spec.member(name)
                self.assertIsNotNone(m, f"missing member: {name}")
                self.assertEqual(m.agent, agent)
                self.assertEqual(m.mode, mode)

    def test_leader_is_developer(self) -> None:
        # The leader is the only member that receives bare user input.
        # If this flips, the workflow breaks: the user's first message
        # would land on the checker or reviewer instead of the developer.
        spec = load_team_spec(_TEAM_DIR)
        self.assertEqual(spec.leader, "developer")

    def test_checker_is_only_default_mode(self) -> None:
        # The communication graph relies on checker being the only
        # `default`-mode (always-wake) member; the other two stay silent
        # so they wake only on @-mention.
        spec = load_team_spec(_TEAM_DIR)
        defaults = [m.name for m in spec.members if m.mode == "default"]
        self.assertEqual(defaults, ["checker"])


class DevelopMachineRoleAgentTests(unittest.TestCase):
    def test_role_agent_dirs_exist(self) -> None:
        for role in _ROLE_AGENTS:
            with self.subTest(role=role):
                d = _AGENT_BASE / role
                self.assertTrue((d / "config.yaml").exists(), f"{role}: config.yaml")
                self.assertTrue((d / "tools.md").exists(), f"{role}: tools.md")
                self.assertTrue(
                    (d / "prompts" / "system.md").exists(),
                    f"{role}: prompts/system.md",
                )

    def test_role_agents_load(self) -> None:
        # Each role must be loadable via the shared AgentLoader path —
        # the same code that the team router calls to spawn member
        # sessions. If a role agent's config.yaml drifts (e.g. points
        # at a missing prompt file) this catches it.
        loader = AgentLoader()
        for role in _ROLE_AGENTS:
            with self.subTest(role=role):
                agent = loader.load(_AGENT_BASE / role)
                self.assertTrue(agent.system_prompt, f"{role}: empty system prompt")

    def test_role_tools_include_teamchat(self) -> None:
        # Members coordinate via teamchat — without these two tools the
        # role is mute in the team.
        for role in _ROLE_AGENTS:
            with self.subTest(role=role):
                tools = (_AGENT_BASE / role / "tools.md").read_text(
                    encoding="utf-8"
                ).split()
                self.assertIn("teamchat_send", tools)
                self.assertIn("teamchat_view", tools)


class DevelopMachineCommunicationRulesTests(unittest.TestCase):
    """Prompt-level invariants for the "checker is the only hub" rule.

    The runtime can't enforce who-talks-to-whom (teamchat is broadcast
    plus mode-based wakeup), so these rules live in the prompts. The
    tests confirm the prompts actually say so, in case a future edit
    accidentally removes the guard.
    """

    def _prompt(self, role: str) -> str:
        return (_AGENT_BASE / role / "prompts" / "system.md").read_text(
            encoding="utf-8"
        )

    def test_developer_prompt_forbids_reviewer_address(self) -> None:
        body = self._prompt("dm_developer")
        # "@reviewer" must appear in a forbidding context — we just
        # require the literal "Never `@reviewer`" guard the prompt
        # encodes today. If you rephrase the rule, update this test.
        self.assertIn("Never `@reviewer`", body)
        self.assertIn("@checker", body)

    def test_reviewer_prompt_forbids_developer_address(self) -> None:
        body = self._prompt("dm_reviewer")
        self.assertIn("Never `@developer`", body)
        self.assertIn("@checker", body)

    def test_checker_prompt_addresses_both_sides(self) -> None:
        body = self._prompt("dm_checker")
        self.assertIn("@developer", body)
        self.assertIn("@reviewer", body)


if __name__ == "__main__":
    unittest.main()
