from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Skill:
    """A skill provides specialized knowledge an agent can activate on demand.

    Follows the Agent Skills specification (https://agentskills.io/specification).

    Attributes:
        name:        Skill identifier (matches its directory name).
        description: When and why to use this skill. This is the primary
                     activation trigger — the model reads descriptions to decide
                     which skill (if any) to load.
        when_to_use: Optional extended trigger guidance. Mirrors Claude Code's
                     richer skill frontmatter and is rendered in the catalog
                     when present.
        body:        Markdown body (frontmatter stripped). Injected inline when
                     no ``location`` is set (e.g. programmatically created skills).
        location:    Absolute path to the SKILL.md file. When set, the skill is
                     listed in the system-prompt catalog and the model reads the
                     file on demand (progressive disclosure). When None, ``body``
                     is injected directly into the system prompt.
        metadata:    Full parsed frontmatter for advanced skill executors.
    """

    name: str
    description: str
    when_to_use: str = ""
    body: str = ""
    location: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def root_dir(self) -> Path | None:
        """Return the skill directory when this skill is file-backed."""
        if self.location is None:
            return None
        return self.location.parent
