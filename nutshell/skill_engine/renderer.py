from __future__ import annotations
from nutshell.core.skill import Skill


def build_skills_block(skills: list[Skill]) -> str:
    """Render a list of skills into a system prompt block.

    File-backed skills (with a ``location``) get a catalog entry for
    progressive disclosure — the model reads SKILL.md on demand via its
    file/bash tool.  Inline skills (no location) have their body injected
    directly into the system prompt.

    Returns an empty string when there are no skills.
    """
    if not skills:
        return ""

    parts: list[str] = []

    # File-backed skills → progressive disclosure: catalog only.
    file_skills = [s for s in skills if s.location is not None]
    if file_skills:
        catalog = ["<available_skills>"]
        for s in file_skills:
            catalog.append(
                f"  <skill>\n"
                f"    <name>{s.name}</name>\n"
                f"    <description>{s.description}</description>\n"
                f"    <location>{s.location}</location>\n"
                f"  </skill>"
            )
        catalog.append("</available_skills>")
        parts.append(
            "\n\n# Available Skills\n"
            "When a task matches a skill's description, read the SKILL.md "
            "at the listed location before proceeding.\n\n"
            + "\n".join(catalog)
        )

    # Inline skills (no file on disk) → inject body directly.
    for s in skills:
        if s.location is not None:
            continue
        header = f"\n\n# Skill: {s.name}"
        if s.description:
            header += f"\n{s.description}"
        parts.append(f"{header}\n\n{s.body}")

    return "\n".join(parts)
