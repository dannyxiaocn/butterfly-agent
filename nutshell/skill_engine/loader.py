from __future__ import annotations
from pathlib import Path

from nutshell.core.loader import BaseLoader
from nutshell.core.skill import Skill


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SKILLHUB_DIR = _REPO_ROOT / "skillhub"


def _read_skills_md(path: Path) -> list[str]:
    """Read skills.md and return list of skill names (one per line, stripped, no blanks)."""
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body. Returns (metadata_dict, body_str)."""
    if not text.startswith("---"):
        return {}, text

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    end_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, text

    meta_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:]).strip()
    try:
        import yaml
    except ImportError:
        raise ImportError("Install pyyaml to use SkillLoader: pip install pyyaml")
    try:
        meta = yaml.safe_load(meta_text)
    except yaml.YAMLError:
        meta = None
    if not isinstance(meta, dict):
        meta = {}
    return meta, body


def _load_skill_file(path: Path) -> Skill:
    """Parse a SKILL.md (or legacy .md) file and return a Skill."""
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    default_name = path.parent.stem if path.name == "SKILL.md" else path.stem
    name = meta.get("name") or default_name
    description = str(meta.get("description") or "").strip()
    when_to_use = str(meta.get("when_to_use") or meta.get("when-to-use") or "").strip()
    if not description and when_to_use:
        description = when_to_use
    return Skill(
        name=name,
        description=description,
        when_to_use=when_to_use,
        body=body,
        location=path.resolve(),
        metadata=meta,
    )


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
            elif p.is_file() and p.suffix == ".md" and p.name.upper() != "README.MD":
                skills.append(self.load(p))
        return skills

    def load_from_skillhub(self, name: str) -> Skill | None:
        """Load a single skill from skillhub by name.

        Looks for ``skillhub/<name>/SKILL.md``. Returns ``None`` if
        the directory or file does not exist.
        """
        skill_md = _SKILLHUB_DIR / name / "SKILL.md"
        if not skill_md.exists():
            return None
        return _load_skill_file(skill_md)

    def load_from_skills_md(self, skills_md_path: Path) -> list[Skill]:
        """Load all skills listed in a skills.md file."""
        names = _read_skills_md(skills_md_path)
        skills: list[Skill] = []
        for name in names:
            skill = self.load_from_skillhub(name)
            if skill is not None:
                skills.append(skill)
            else:
                print(f"[skill_engine] Warning: skill '{name}' not found in skillhub")
        return skills
