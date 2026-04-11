from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from nutshell.core.skill import Skill
from nutshell.skill_engine.loader import SkillLoader
from nutshell.skill_engine.renderer import build_skills_block


class SkillLoaderUnitTests(unittest.TestCase):
    def test_load_dir_ignores_readme_and_loads_supported_skills(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "README.md").write_text("catalog", encoding="utf-8")
            (root / "legacy.md").write_text("---\nname: legacy\ndescription: legacy skill\n---\nbody", encoding="utf-8")
            (root / "dirskill").mkdir()
            (root / "dirskill" / "SKILL.md").write_text(
                "---\nname: dirskill\ndescription: dir skill\n---\nbody",
                encoding="utf-8",
            )

            skills = SkillLoader().load_dir(root)

        self.assertEqual([skill.name for skill in skills], ["dirskill", "legacy"])

    def test_renderer_escapes_file_backed_catalog_entries(self) -> None:
        skill = Skill(
            name="xml<skill>",
            description="Use <carefully> & well",
            when_to_use="when a<b",
            location=Path("/tmp/skill/SKILL.md"),
        )

        block = build_skills_block([skill])

        self.assertIn("xml&lt;skill&gt;", block)
        self.assertIn("Use &lt;carefully&gt; &amp; well", block)
        self.assertIn("when a&lt;b", block)

