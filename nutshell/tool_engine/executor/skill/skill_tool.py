from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Iterable

from nutshell.core.skill import Skill
from nutshell.core.tool import Tool
from nutshell.tool_engine.executor.base import BaseExecutor


def _normalize_skill_name(name: str) -> str:
    return name.strip().lstrip("/")


def _split_args(args: str | None) -> list[str]:
    if not args or not args.strip():
        return []
    try:
        return shlex.split(args)
    except ValueError:
        return args.split()


def _substitute_skill_vars(text: str, skill: Skill, args: str | None) -> str:
    root_dir = skill.root_dir
    if root_dir is not None:
        skill_dir = root_dir.as_posix()
        for var in ("${NUTSHELL_SKILL_DIR}", "${CLAUDE_SKILL_DIR}", "${CODEX_SKILL_DIR}"):
            text = text.replace(var, skill_dir)

    raw_args = (args or "").strip()
    text = text.replace("${NUTSHELL_SKILL_ARGS}", raw_args)
    text = text.replace("${CLAUDE_SKILL_ARGS}", raw_args)
    text = text.replace("${CODEX_SKILL_ARGS}", raw_args)
    text = text.replace("$ARGUMENTS", raw_args)

    arg_names = skill.metadata.get("arguments")
    if isinstance(arg_names, str):
        arg_names = [arg_names]
    if isinstance(arg_names, list):
        values = _split_args(args)
        # Build pairs and sort by name length descending so $topic is replaced
        # before $to, preventing substring collisions.
        pairs: list[tuple[str, str]] = []
        for idx, raw_name in enumerate(arg_names):
            if not isinstance(raw_name, str):
                continue
            name = raw_name.strip().lstrip("$")
            if not name:
                continue
            value = values[idx] if idx < len(values) else ""
            pairs.append((name, value))
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        for name, value in pairs:
            text = text.replace(f"${{{name}}}", value)
            text = text.replace(f"${name}", value)

    return text


def _list_related_files(root_dir: Path | None) -> list[str]:
    if root_dir is None or not root_dir.exists():
        return []

    files: list[str] = []
    for path in sorted(root_dir.rglob("*")):
        if path.is_dir() or path.name == "SKILL.md":
            continue
        rel = path.relative_to(root_dir).as_posix()
        files.append(rel)
    return files


class SkillExecutor(BaseExecutor):
    """Executor for the built-in skill loading tool."""

    def __init__(self, skills: Iterable[Skill] | None = None) -> None:
        self._skills = list(skills or [])

    @classmethod
    def can_handle(cls, tool_name: str, tool_path: Path | None) -> bool:
        return tool_name == "skill"

    def _find_skill(self, name: str) -> Skill | None:
        normalized = _normalize_skill_name(name)
        for skill in self._skills:
            if skill.name == normalized:
                return skill
        return None

    async def execute(self, **kwargs: Any) -> str:
        requested_name = kwargs["skill"]
        args = kwargs.get("args")
        skill = self._find_skill(requested_name)
        if skill is None:
            available = ", ".join(s.name for s in self._skills) or "none"
            return (
                f"Unknown skill: {_normalize_skill_name(requested_name)}\n"
                f"Available skills: {available}"
            )

        rendered_body = _substitute_skill_vars(skill.body, skill, args)
        parts = [f"Loaded skill: {skill.name}"]
        if skill.description:
            parts.append(f"Description: {skill.description}")
        if skill.when_to_use:
            parts.append(f"When to use: {skill.when_to_use}")
        if args:
            parts.append(f"Arguments: {args}")
        if skill.root_dir is not None:
            parts.append(f"Base directory for this skill: {skill.root_dir}")
            related_files = _list_related_files(skill.root_dir)
            if related_files:
                preview = ", ".join(related_files[:20])
                if len(related_files) > 20:
                    preview += f", ... ({len(related_files) - 20} more)"
                parts.append(f"Related files in the skill directory: {preview}")
        parts.append("")
        parts.append(rendered_body)
        return "\n".join(parts).strip()


def create_skill_tool(skills: Iterable[Skill] | None = None) -> Tool:
    executor = SkillExecutor(skills=skills)

    async def skill(skill: str, args: str | None = None) -> str:
        return await executor.execute(skill=skill, args=args)

    return Tool(
        name="skill",
        description=(
            "Load a skill into context. Use this before working on a task when an "
            "available skill matches the request."
        ),
        func=skill,
        schema={
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "The skill name to load, for example `creator-mode`.",
                },
                "args": {
                    "type": "string",
                    "description": "Optional raw argument string for parameterized skills.",
                },
            },
            "required": ["skill"],
        },
    )
