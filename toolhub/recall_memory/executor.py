"""recall_memory tool — reads full content of a memory layer file."""
from __future__ import annotations

from pathlib import Path
from typing import Any


class RecallMemoryExecutor:
    def __init__(self, memory_dir: str | Path | None = None) -> None:
        self._memory_dir = Path(memory_dir) if memory_dir else None

    async def execute(self, **kwargs: Any) -> str:
        if self._memory_dir is None:
            return "Error: memory directory not configured"

        name = kwargs.get("name", "").strip()
        if not name:
            # List available memory layers
            if not self._memory_dir.is_dir():
                return "No memory layers found."
            files = sorted(p.stem for p in self._memory_dir.glob("*.md") if p.read_text(encoding="utf-8").strip())
            if not files:
                return "No memory layers found."
            return "Available memory layers:\n" + "\n".join(f"- {f}" for f in files)

        # Sanitize name
        safe_name = name.replace("/", "").replace("\\", "").replace("..", "")
        path = self._memory_dir / f"{safe_name}.md"
        if not path.exists():
            available = sorted(p.stem for p in self._memory_dir.glob("*.md"))
            avail_str = ", ".join(available) if available else "none"
            return f"Memory layer '{name}' not found. Available: {avail_str}"

        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return f"Memory layer '{name}' is empty."
        return f"# Memory: {name}\n\n{content}"
