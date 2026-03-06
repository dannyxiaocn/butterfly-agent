from __future__ import annotations
from pathlib import Path

from nutshell.base.loader import BaseLoader


class PromptLoader(BaseLoader[str]):
    """Load a Markdown file as a plain string (system prompt)."""

    def load(self, path: Path) -> str:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Prompt file not found: {path}")
        return path.read_text(encoding="utf-8").strip()

    def load_dir(self, directory: Path) -> list[str]:
        directory = Path(directory)
        return [self.load(p) for p in sorted(directory.glob("*.md"))]
