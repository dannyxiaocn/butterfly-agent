from __future__ import annotations
from pathlib import Path

from nutshell.core.loader import BaseLoader
from nutshell.core.skill import Skill


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body. Returns (metadata_dict, body_str)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        import yaml
    except ImportError:
        raise ImportError("Install pyyaml to use SkillLoader: pip install pyyaml")
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        meta = None
    if not isinstance(meta, dict):
        meta = {}
    body = parts[2].strip()
    return meta, body


def _load_skill_file(path: Path) -> Skill:
    """Parse a SKILL.md (or legacy .md) file and return a Skill."""
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    default_name = path.parent.stem if path.name == "SKILL.md" else path.stem
    name = meta.get("name") or default_name
    description = meta.get("description") or ""
    return Skill(name=name, description=description, body=body, location=path)


class SkillLoader(BaseLoader[Skill]):
    """Load Agent Skills from the filesystem.

    Supports two layouts:

    1. **Directory layout** (preferred):
       ``skills/reasoning/SKILL.md``
       Pass the *directory* path; ``SKILL.md`` is discovered automatically.

    2. **Legacy flat file** (backward-compatible):
       ``skills/reasoning.md``
       Pass the file path directly.
    """

    def load(self, path: Path) -> Skill:
        path = Path(path)
        if path.is_dir():
            skill_md = path / "SKILL.md"
            if not skill_md.exists():
                raise FileNotFoundError(f"SKILL.md not found in directory: {path}")
            return _load_skill_file(skill_md)
        if not path.exists():
            raise FileNotFoundError(f"Skill file not found: {path}")
        return _load_skill_file(path)

    def load_dir(self, directory: Path) -> list[Skill]:
        """Load all skills from a directory."""
        directory = Path(directory)
        skills: list[Skill] = []
        for p in sorted(directory.iterdir()):
            if p.is_dir() and (p / "SKILL.md").exists():
                skills.append(self.load(p))
            elif p.is_file() and p.suffix == ".md":
                skills.append(self.load(p))
        return skills
