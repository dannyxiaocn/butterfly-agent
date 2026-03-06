from __future__ import annotations
from pathlib import Path

from nutshell.base.loader import BaseLoader
from nutshell.skill import Skill


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
    meta = yaml.safe_load(parts[1]) or {}
    body = parts[2].strip()
    return meta, body


class SkillLoader(BaseLoader[Skill]):
    """Load a Markdown file with YAML frontmatter as a Skill.

    File format:
        ---
        name: skill-name
        description: What this skill does
        ---

        # Prompt injection content here...
    """

    def load(self, path: Path) -> Skill:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Skill file not found: {path}")
        text = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        name = meta.get("name") or path.stem
        description = meta.get("description") or ""
        return Skill(name=name, description=description, prompt_injection=body)

    def load_dir(self, directory: Path) -> list[Skill]:
        directory = Path(directory)
        return [self.load(p) for p in sorted(directory.glob("*.md"))]
