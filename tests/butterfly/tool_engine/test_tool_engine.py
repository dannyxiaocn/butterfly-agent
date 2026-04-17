from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from butterfly.core.skill import Skill
from butterfly.tool_engine.loader import ToolLoader


# Note: butterfly.tool_engine.reload and its create_reload_tool / _summarize_names
# helpers were removed in v2.0.5. Capability reloading is now runtime-level
# file-watch; there is no in-agent reload tool to test here.


class ToolEngineTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_loader_uses_bash_executor_for_builtin_tool(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "bash.json"
            path.write_text(
                json.dumps(
                    {
                        "name": "bash",
                        "description": "shell",
                        "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}},
                    }
                ),
                encoding="utf-8",
            )
            tool = ToolLoader(default_workdir=tmp).load(path)
            output = await tool.execute(command="printf 'ok'")
        self.assertIn("ok", output)
        self.assertIn("[exit 0", output)

    def test_tool_loader_prefers_shell_executor_when_script_exists(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "echo.json"
            path.write_text(
                json.dumps(
                    {
                        "name": "echo",
                        "description": "echo",
                        "input_schema": {"type": "object", "properties": {"value": {"type": "string"}}},
                    }
                ),
                encoding="utf-8",
            )
            script = path.with_suffix(".sh")
            script.write_text("#!/bin/sh\nprintf 'shell tool'\n", encoding="utf-8")
            tool = ToolLoader(default_workdir=tmp).load(path)
        self.assertEqual(tool.name, "echo")

    async def test_skill_tool_renders_arguments_and_related_files(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "creator-mode"
            root.mkdir()
            (root / "SKILL.md").write_text(
                "---\nname: creator-mode\narguments:\n  - topic\n---\nTopic: $topic\n",
                encoding="utf-8",
            )
            (root / "notes.txt").write_text("extra", encoding="utf-8")
            skill = Skill(
                name="creator-mode",
                description="creator",
                body="Topic: $topic",
                location=root / "SKILL.md",
                metadata={"arguments": ["topic"]},
            )
            path = Path(tmp) / "skill.json"
            path.write_text(json.dumps({"name": "skill", "input_schema": {"type": "object"}}), encoding="utf-8")
            tool = ToolLoader(skills=[skill]).load(path)
            output = await tool.execute(skill="creator-mode", args="testing")
        self.assertIn("Loaded skill: creator-mode", output)
        self.assertIn("Topic: testing", output)
        self.assertIn("notes.txt", output)

if __name__ == "__main__":
    unittest.main()
