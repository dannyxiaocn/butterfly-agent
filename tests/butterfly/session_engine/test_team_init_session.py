"""Integration test: init_team_session lays out the team + member sessions.

Validates:

  * Team manifest is written at _sessions/<team_id>/manifest.json with
    kind=team, leader, members[], members_map.
  * One child session is spawned per member, each with member_of_team /
    member_name / teamchat_mode in its own manifest.
  * One TYPE_SUB_AGENT panel entry is written per member under
    sessions/<team_id>/core/panel/.
  * teamchat.jsonl is created (empty) in the team's core dir.
  * The function is idempotent — re-invoking with the same team_id but
    after one member has been added doesn't duplicate the existing member
    rows.
"""
from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def _build_agent_dir(agent_base: Path, name: str) -> None:
    d = agent_base / name
    (d / "prompts").mkdir(parents=True)
    (d / "tools.md").write_text("", encoding="utf-8")
    (d / "skills.md").write_text("", encoding="utf-8")
    (d / "prompts" / "system.md").write_text("system", encoding="utf-8")
    (d / "prompts" / "task.md").write_text("task", encoding="utf-8")
    (d / "prompts" / "env.md").write_text("env", encoding="utf-8")
    (d / "config.yaml").write_text(
        "\n".join(
            [
                "prompts:",
                "  system: prompts/system.md",
                "  task: prompts/task.md",
                "  env: prompts/env.md",
                "provider: anthropic",
                "model: demo",
            ]
        ),
        encoding="utf-8",
    )


def _build_team_dir(agent_base: Path, name: str, members: list[dict]) -> None:
    d = agent_base / name
    d.mkdir(parents=True)
    body = ["kind: team", f"agent: {name}", "leader: planner", "members:"]
    for m in members:
        body.append(f"  - name: {m['name']}")
        body.append(f"    agent: {m['agent']}")
        body.append(f"    teamchat_mode: {m['mode']}")
    (d / "config.yaml").write_text("\n".join(body), encoding="utf-8")


class InitTeamSessionTests(unittest.TestCase):
    def test_team_session_layout(self) -> None:
        from butterfly.session_engine.session_init import init_team_session

        team_name = f"unit_team_{uuid.uuid4().hex[:6]}"
        agent_a = f"a_{uuid.uuid4().hex[:6]}"
        agent_b = f"b_{uuid.uuid4().hex[:6]}"

        with TemporaryDirectory() as td, patch(
            "butterfly.session_engine.session_init._create_session_venv",
            side_effect=lambda p: p / ".venv",
        ):
            root = Path(td)
            agent_base = root / "agenthub"
            sessions_base = root / "sessions"
            system_base = root / "_sessions"
            agent_base.mkdir(parents=True)

            _build_agent_dir(agent_base, agent_a)
            _build_agent_dir(agent_base, agent_b)
            _build_team_dir(agent_base, team_name, [
                {"name": "planner", "agent": agent_a, "mode": "default"},
                {"name": "coder",   "agent": agent_b, "mode": "silent"},
            ])

            team_id = "team_test"
            returned_id, members_map = init_team_session(
                team_id,
                team_name,
                sessions_base=sessions_base,
                system_sessions_base=system_base,
                agent_base=agent_base,
            )
            self.assertEqual(returned_id, team_id)
            self.assertEqual(set(members_map.keys()), {"planner", "coder"})

            # Team manifest
            team_manifest = json.loads(
                (system_base / team_id / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(team_manifest["kind"], "team")
            self.assertEqual(team_manifest["leader"], "planner")
            self.assertEqual(team_manifest["team_name"], team_name)
            self.assertEqual(team_manifest["members_map"], members_map)

            # teamchat.jsonl created
            self.assertTrue(
                (sessions_base / team_id / "core" / "teamchat.jsonl").exists()
            )

            # Each member's child session manifest carries the membership fields
            for name, mode in (("planner", "default"), ("coder", "silent")):
                mid = members_map[name]
                m_manifest = json.loads(
                    (system_base / mid / "manifest.json").read_text(encoding="utf-8")
                )
                self.assertEqual(m_manifest["member_of_team"], team_id)
                self.assertEqual(m_manifest["member_name"], name)
                self.assertEqual(m_manifest["teamchat_mode"], mode)
                self.assertEqual(m_manifest["parent_session_id"], team_id)

            # Panel cards: one TYPE_SUB_AGENT row per member
            panel_dir = sessions_base / team_id / "core" / "panel"
            panel_entries = sorted(panel_dir.glob("*.json"))
            self.assertEqual(len(panel_entries), 2)
            members_in_panel = set()
            for p in panel_entries:
                d = json.loads(p.read_text(encoding="utf-8"))
                self.assertEqual(d["type"], "sub_agent")
                self.assertEqual(d["tool_name"], "team_member")
                members_in_panel.add(d["meta"]["display_name"])
                self.assertEqual(d["meta"]["kind"], "team_member")
                self.assertIn(d["meta"]["display_name"], members_map)
            self.assertEqual(members_in_panel, {"planner", "coder"})

            # Idempotency — calling again returns the same map; no duplicate
            # children, no duplicate panel rows.
            same_id, second_map = init_team_session(
                team_id,
                team_name,
                sessions_base=sessions_base,
                system_sessions_base=system_base,
                agent_base=agent_base,
            )
            self.assertEqual(same_id, team_id)
            self.assertEqual(second_map, members_map)
            self.assertEqual(
                len(sorted((sessions_base / team_id / "core" / "panel").glob("*.json"))),
                2,
            )


if __name__ == "__main__":
    unittest.main()
