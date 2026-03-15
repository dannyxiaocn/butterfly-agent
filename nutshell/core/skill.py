from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Skill:
    """A skill provides specialized knowledge an agent can activate on demand.

    Follows the Agent Skills specification (https://agentskills.io/specification).

    Attributes:
        name:        Skill identifier (matches its directory name).
        description: When and why to use this skill. This is the primary
                     activation trigger — the model reads descriptions to decide
                     which skill (if any) to load.
        body:        Markdown body (frontmatter stripped). Injected inline when
                     no ``location`` is set (e.g. programmatically created skills).
        location:    Absolute path to the SKILL.md file. When set, the skill is
                     listed in the system-prompt catalog and the model reads the
                     file on demand (progressive disclosure). When None, ``body``
                     is injected directly into the system prompt.
    """

    name: str
    description: str
    body: str = ""
    location: Path | None = None
