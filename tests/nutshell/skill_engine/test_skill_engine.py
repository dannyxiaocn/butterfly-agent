from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from nutshell.core.skill import Skill
from nutshell.skill_engine.loader import SkillLoader, _parse_frontmatter
from nutshell.skill_engine.renderer import build_skills_block


class SkillEngineTest(unittest.TestCase):
    def test_parse_frontmatter_extracts_metadata_and_body(self) -> None:
        meta, body = _parse_frontmatter(
            "---\nname: alpha\ndescription: use alpha\nwhen_to_use: for alpha\n---\n\nbody\n"
        )
        self.assertEqual(meta["name"], "alpha")
        self.assertEqual(body, "body")

    def test_parse_frontmatter_handles_invalid_yaml(self) -> None:
        meta, body = _parse_frontmatter("---\n[\n---\nbody")
        self.assertEqual(meta, {})
        self.assertEqual(body, "body")

    def test_skill_loader_supports_directory_and_legacy_file_layouts(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            modern = root / "modern"
            modern.mkdir()
            (modern / "SKILL.md").write_text("---\nname: modern\n---\nmodern body", encoding="utf-8")
            (root / "legacy.md").write_text("---\nname: legacy\n---\nlegacy body", encoding="utf-8")
            skills = SkillLoader().load_dir(root)
        self.assertEqual([skill.name for skill in skills], ["legacy", "modern"])

    def test_renderer_separates_file_backed_and_inline_skills(self) -> None:
        file_skill = Skill(
            name="repo",
            description="repo work",
            when_to_use="when repository context matters",
            body="ignored inline",
            location=Path("/tmp/repo/SKILL.md"),
        )
        inline_skill = Skill(name="inline", description="inline desc", body="inline body")
        rendered = build_skills_block([file_skill, inline_skill])
        self.assertIn("<available_skills>", rendered)
        self.assertIn("repo work", rendered)
        self.assertIn("Skill: inline", rendered)
        self.assertIn("inline body", rendered)


if __name__ == "__main__":
    unittest.main()
