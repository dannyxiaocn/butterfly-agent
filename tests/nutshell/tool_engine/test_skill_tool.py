"""Tests for the built-in skill tool."""
from __future__ import annotations

import json

import pytest

from nutshell.core.tool import Tool
from nutshell.skill_engine.loader import SkillLoader
from nutshell.tool_engine.executor.skill.skill_tool import create_skill_tool
from nutshell.tool_engine.loader import ToolLoader


def _write_skill_json(path):
    path.write_text(json.dumps({
        "name": "skill",
        "description": "load a skill",
        "input_schema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string"},
                "args": {"type": "string"},
            },
            "required": ["skill"],
        },
    }))


def test_create_skill_tool_returns_tool():
    t = create_skill_tool()
    assert isinstance(t, Tool)
    assert t.name == "skill"


@pytest.mark.asyncio
async def test_toolloader_skill_loads_skill_body(tmp_path):
    skill_dir = tmp_path / "skills" / "reasoning"
    skill_dir.mkdir(parents=True)
    (skill_dir / "notes.md").write_text("extra context", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: reasoning\ndescription: Better reasoning\n---\n\nUse chain-of-thought carefully.\n",
        encoding="utf-8",
    )
    skill = SkillLoader().load(skill_dir)

    tool_json = tmp_path / "tools" / "skill.json"
    tool_json.parent.mkdir()
    _write_skill_json(tool_json)

    tool = ToolLoader(skills=[skill]).load(tool_json)
    result = await tool.execute(skill="reasoning")

    assert "Loaded skill: reasoning" in result
    assert "Better reasoning" in result
    assert "Use chain-of-thought carefully." in result
    assert f"Base directory for this skill: {skill_dir.resolve()}" in result
    assert "notes.md" in result


@pytest.mark.asyncio
async def test_skill_tool_unknown_skill_lists_available(tmp_path):
    skill_dir = tmp_path / "skills" / "math"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: math\ndescription: Math help\n---\n\nDo math.\n",
        encoding="utf-8",
    )
    skill = SkillLoader().load(skill_dir)

    tool_json = tmp_path / "tools" / "skill.json"
    tool_json.parent.mkdir()
    _write_skill_json(tool_json)

    tool = ToolLoader(skills=[skill]).load(tool_json)
    result = await tool.execute(skill="unknown")

    assert "Unknown skill: unknown" in result
    assert "Available skills: math" in result


@pytest.mark.asyncio
async def test_skill_tool_substitutes_dir_and_arguments(tmp_path):
    skill_dir = tmp_path / "skills" / "templated"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: templated\ndescription: Uses placeholders\narguments:\n  - topic\n---\n\nDir: ${CLAUDE_SKILL_DIR}\nTopic: $topic\nAll: $ARGUMENTS\n",
        encoding="utf-8",
    )
    skill = SkillLoader().load(skill_dir)

    tool_json = tmp_path / "tools" / "skill.json"
    tool_json.parent.mkdir()
    _write_skill_json(tool_json)

    tool = ToolLoader(skills=[skill]).load(tool_json)
    result = await tool.execute(skill="templated", args="refactor")

    assert f"Dir: {skill_dir.resolve().as_posix()}" in result
    assert "Topic: refactor" in result
    assert "All: refactor" in result
