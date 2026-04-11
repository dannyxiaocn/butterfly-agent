from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ui.cli.friends import build_friends_list
from ui.cli.kanban import build_kanban, format_kanban_json
from ui.cli.main import _parse_inject_memory, _write_inject_memory
from ui.cli.new_agent import create_entity
from ui.cli.visit import format_room_text, gather_room_data


class CliHelpersTest(unittest.TestCase):
    def test_parse_and_write_inject_memory(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "memory.txt"
            source.write_text("from file", encoding="utf-8")
            parsed = _parse_inject_memory(["alpha=value", f"beta=@{source}"])
            self.assertEqual(parsed["alpha"], "value")
            self.assertEqual(parsed["beta"], "from file")

            session_dir = root / "sessions" / "demo"
            _write_inject_memory(session_dir, parsed)
            self.assertEqual(
                (session_dir / "core" / "memory" / "alpha.md").read_text(encoding="utf-8"),
                "value",
            )

    def test_build_friends_list_groups_running_sessions_first(self) -> None:
        friends = build_friends_list(
            [
                {"id": "idle", "entity": "agent", "pid_alive": False, "status": "active", "model_state": "idle"},
                {"id": "run", "entity": "agent", "pid_alive": True, "status": "active", "model_state": "running"},
            ]
        )
        self.assertEqual(friends[0]["id"], "run")

    def test_kanban_helpers_keep_open_tasks(self) -> None:
        with TemporaryDirectory() as tmp:
            sessions_base = Path(tmp) / "sessions"
            tasks_dir = sessions_base / "demo" / "core" / "tasks"
            tasks_dir.mkdir(parents=True)
            (tasks_dir / "heartbeat.md").write_text(
                "---\ninterval: 60\nstatus: pending\n---\n\nship it\n",
                encoding="utf-8",
            )
            entries = build_kanban(
                [{"id": "demo", "entity": "agent", "pid_alive": True, "status": "active", "model_state": "running"}],
                sessions_base,
            )
        self.assertEqual(entries[0]["id"], "demo")
        self.assertIn("ship it", entries[0]["tasks_content"])
        self.assertIn('"id": "demo"', format_kanban_json(entries))

    def test_create_entity_scaffolds_standalone_and_inheriting_layouts(self) -> None:
        with TemporaryDirectory() as tmp:
            entity_root = Path(tmp) / "entity"
            (entity_root / "agent" / "prompts").mkdir(parents=True)
            (entity_root / "agent" / "tools").mkdir()
            (entity_root / "agent" / "agent.yaml").write_text("name: agent\n", encoding="utf-8")
            (entity_root / "agent" / "prompts" / "system.md").write_text("sys", encoding="utf-8")
            (entity_root / "agent" / "prompts" / "heartbeat.md").write_text("beat", encoding="utf-8")
            (entity_root / "agent" / "prompts" / "session.md").write_text("sess", encoding="utf-8")
            (entity_root / "agent" / "tools" / "bash.json").write_text('{"name":"bash"}', encoding="utf-8")
            (entity_root / "agent" / "tools" / "web_search.json").write_text('{"name":"web_search"}', encoding="utf-8")
            standalone = create_entity("standalone", entity_root, None)
            child = create_entity("child", entity_root, "agent")
            self.assertTrue((standalone / "prompts" / "system.md").exists())
            self.assertTrue((standalone / "tools" / "bash.json").exists())
            self.assertTrue((child / "tools" / ".gitkeep").exists())

    def test_visit_gather_room_data_and_format_text(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions_base = root / "sessions"
            system_base = root / "_sessions"
            session_dir = sessions_base / "demo"
            system_dir = system_base / "demo"
            (session_dir / "core" / "apps").mkdir(parents=True)
            system_dir.mkdir(parents=True)
            (system_dir / "manifest.json").write_text(json.dumps({"entity": "agent"}), encoding="utf-8")
            (system_dir / "status.json").write_text(json.dumps({"status": "active", "model_state": "idle"}), encoding="utf-8")
            (system_dir / "context.jsonl").write_text(
                json.dumps({"type": "user_input", "content": "hello"}) + "\n"
                + json.dumps({"type": "turn", "messages": [{"role": "assistant", "content": "world"}]}),
                encoding="utf-8",
            )
            (session_dir / "core" / "tasks").mkdir(parents=True)
            (session_dir / "core" / "tasks" / "heartbeat.md").write_text(
                "---\ninterval: 60\nstatus: pending\n---\n\nship it\n",
                encoding="utf-8",
            )
            (session_dir / "core" / "apps" / "mail.md").write_text("1 unread", encoding="utf-8")
            data = gather_room_data("demo", sessions_base=sessions_base, system_base=system_base)
            text = format_room_text(data)
        self.assertEqual(data["entity"], "agent")
        self.assertIn("ship it", text)
        self.assertIn("[mail]", text)


if __name__ == "__main__":
    unittest.main()
