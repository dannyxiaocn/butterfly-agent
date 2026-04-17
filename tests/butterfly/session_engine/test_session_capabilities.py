"""Tests for Session._load_session_capabilities:
- Agent-created (session-scoped) tools in sessions/<id>/core/tools/
- Session skills in sessions/<id>/core/skills/
- Memory injection from sessions/<id>/core/memory.md
- No duplication on repeated capability reloads
"""
import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path

from butterfly.core.provider import Provider
from butterfly.core.agent import Agent
from butterfly.core.skill import Skill
from butterfly.core.tool import tool
from butterfly.session_engine.session_config import write_config
from butterfly.session_engine.session import Session
from butterfly.session_engine.task_cards import TaskCard, save_card


class MockProvider(Provider):
    def __init__(self, responses):
        self._responses = iter(responses)

    async def complete(self, messages, tools, system_prompt, model, *, on_text_chunk=None, cache_system_prefix="", cache_last_human_turn=False, thinking: bool = False, thinking_budget: int = 8000, thinking_effort: str = "high", on_thinking_start=None, on_thinking_end=None):
        from butterfly.core.types import TokenUsage
        r = next(self._responses)
        return (r[0], r[1], r[2] if len(r) > 2 else TokenUsage())


def make_session(tmp_path: Path, agent: Agent, session_id: str = "test") -> Session:
    system_base = tmp_path / "_sessions"
    session = Session(agent, session_id=session_id, base_dir=tmp_path, system_base=system_base)
    # Pre-populate core/ prompt files that _load_session_capabilities reads
    (session.core_dir / "system.md").write_text(agent.system_prompt or "", encoding="utf-8")
    (session.core_dir / "task.md").write_text(
        getattr(agent, "task_prompt", "") or "", encoding="utf-8"
    )
    (session.core_dir / "env.md").write_text(
        getattr(agent, "env_template", "") or "", encoding="utf-8"
    )
    return session


def write_tool_files(tools_dir: Path, name: str, description: str = "", sh_body: str = '#!/bin/bash\necho "ok"') -> None:
    schema = {
        "name": name,
        "description": description or name,
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }
    (tools_dir / f"{name}.json").write_text(json.dumps(schema))
    sh = tools_dir / f"{name}.sh"
    sh.write_text(sh_body)
    sh.chmod(0o755)


# ── Session-scoped tools ──────────────────────────────────────────────────────

def test_session_tool_added(tmp_path):
    """New JSON+SH in core/tools/ is picked up by _load_session_capabilities."""
    @tool(description="agent tool")
    def agent_tool() -> str:
        return "agent_tool_result"

    agent = Agent(system_prompt="Base", tools=[agent_tool], provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    write_tool_files(session.core_dir / "tools", "my_new_tool")
    session._load_session_capabilities()

    names = {t.name for t in agent.tools}
    assert "my_new_tool" in names


def test_session_tool_updated_on_reload(tmp_path):
    """Updating a session tool's JSON updates it on next _load_session_capabilities."""
    agent = Agent(system_prompt="Base", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    write_tool_files(session.core_dir / "tools", "my_tool", description="first description")
    session._load_session_capabilities()

    matching = [t for t in agent.tools if t.name == "my_tool"]
    assert len(matching) == 1
    assert matching[0].description == "first description"

    # Update the tool description
    write_tool_files(session.core_dir / "tools", "my_tool", description="updated description")
    session._load_session_capabilities()

    matching = [t for t in agent.tools if t.name == "my_tool"]
    assert len(matching) == 1
    assert matching[0].description == "updated description"


@pytest.mark.asyncio
async def test_session_tool_executes_via_shell(tmp_path):
    """Shell-backed session tool receives JSON on stdin and returns stdout."""
    agent = Agent(system_prompt="Base", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    tool_schema = {
        "name": "echo_tool",
        "description": "Echoes the msg field",
        "input_schema": {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
        },
    }
    sh_body = '#!/bin/bash\npython3 -c "import sys,json; d=json.load(sys.stdin); print(d[\'msg\'])"'
    tools_dir = session.core_dir / "tools"
    (tools_dir / "echo_tool.json").write_text(json.dumps(tool_schema))
    sh = tools_dir / "echo_tool.sh"
    sh.write_text(sh_body)
    sh.chmod(0o755)

    session._load_session_capabilities()
    loaded = next(t for t in agent.tools if t.name == "echo_tool")
    result = await loaded.execute(msg="hello butterfly")
    assert "hello butterfly" in result


def test_no_tool_duplication_on_repeated_loads(tmp_path):
    """Calling _load_session_capabilities twice does not duplicate session tools."""
    agent = Agent(system_prompt="Base", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    write_tool_files(session.core_dir / "tools", "dynamic_tool")

    session._load_session_capabilities()
    count_after_first = sum(1 for t in agent.tools if t.name == "dynamic_tool")

    session._load_session_capabilities()
    count_after_second = sum(1 for t in agent.tools if t.name == "dynamic_tool")

    assert count_after_first == 1
    assert count_after_second == 1


# ── Session skills ────────────────────────────────────────────────────────────

def test_session_skill_added(tmp_path):
    """Skill file placed in core/skills/ is loaded by _load_session_capabilities."""
    agent = Agent(system_prompt="Base", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    skill_dir = session.core_dir / "skills" / "new_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: new_skill\ndescription: A new skill\n---\n\nNew skill body.\n"
    )

    session._load_session_capabilities()

    names = {s.name for s in agent.skills}
    assert "new_skill" in names


def test_session_skill_loaded(tmp_path):
    """Skills from core/skills/ are loaded correctly."""
    agent = Agent(system_prompt="Base", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    skill_dir = session.core_dir / "skills" / "reasoning"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: reasoning\ndescription: improved reasoning\n---\n\nImproved body.\n"
    )

    session._load_session_capabilities()

    matching = [s for s in agent.skills if s.name == "reasoning"]
    assert len(matching) == 1
    assert matching[0].description == "improved reasoning"


def test_session_skill_order_preserved(tmp_path):
    """Skills from core/skills/ are loaded in sorted order."""
    agent = Agent(system_prompt="Base", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    for name in ("alpha", "beta", "gamma"):
        skill_dir = session.core_dir / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name}\n---\n\n{name} body.\n"
        )

    session._load_session_capabilities()

    names = [s.name for s in agent.skills]
    assert names.index("alpha") < names.index("beta") < names.index("gamma")


@pytest.mark.asyncio
async def test_session_skill_tool_loads_session_skill(tmp_path):
    """The built-in skill tool can load a session skill after capability reload."""
    agent = Agent(system_prompt="Base", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    skill_dir = session.core_dir / "skills" / "reasoning"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: reasoning\ndescription: improved reasoning\n---\n\nImproved body.\n"
    )

    tool_schema = {
        "name": "skill",
        "description": "load skills",
        "input_schema": {
            "type": "object",
            "properties": {"skill": {"type": "string"}},
            "required": ["skill"],
        },
    }
    (session.core_dir / "tools" / "skill.json").write_text(json.dumps(tool_schema))

    session._load_session_capabilities()

    skill_tool = next(t for t in agent.tools if t.name == "skill")
    result = await skill_tool.execute(skill="reasoning")
    assert "Loaded skill: reasoning" in result
    assert "Improved body." in result


# ── Memory injection ──────────────────────────────────────────────────────────

def test_memory_injected_into_system_prompt(tmp_path):
    """Content of memory.md is injected into the assembled prompt each activation."""
    agent = Agent(system_prompt="Base system prompt.", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    session.memory_path.write_text("Remember: always be concise.")
    session._load_session_capabilities()

    full_prompt = "\n".join(p for p in agent._build_system_parts() if p)
    assert "Base system prompt." in full_prompt
    assert "Remember: always be concise." in full_prompt


def test_empty_memory_not_injected(tmp_path):
    """Empty memory.md does not append any extra block."""
    agent = Agent(system_prompt="Base.", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    session.memory_path.write_text("")
    session._load_session_capabilities()

    assert "Session Memory" not in "\n".join(p for p in agent._build_system_parts() if p)


def test_memory_cleared_removes_block(tmp_path):
    """After clearing memory.md the block is gone on next build."""
    agent = Agent(system_prompt="Base.", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    session.memory_path.write_text("Some memory.")
    session._load_session_capabilities()
    assert "Some memory." in "\n".join(p for p in agent._build_system_parts() if p)

    session.memory_path.write_text("")
    session._load_session_capabilities()
    assert "Some memory." not in "\n".join(p for p in agent._build_system_parts() if p)


def test_memory_updated_reflects_on_next_load(tmp_path):
    """After updating memory.md the new content appears on next build."""
    agent = Agent(system_prompt="Base.", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    session.memory_path.write_text("First memory.")
    session._load_session_capabilities()
    assert "First memory." in "\n".join(p for p in agent._build_system_parts() if p)

    session.memory_path.write_text("Second memory.")
    session._load_session_capabilities()
    assert "Second memory." in "\n".join(p for p in agent._build_system_parts() if p)
    assert "First memory." not in "\n".join(p for p in agent._build_system_parts() if p)


# ── Layered memory (core/memory/) removed in v2.0.5 ──────────────────────────
# The `memory_layers` feature was replaced by on-demand `memory_recall` tool;
# auto-injection of core/memory/*.md into the system prompt is gone.
# See toolhub/memory_recall/ and tests/butterfly/tool_engine/test_toolhub.py
# (TestMemoryRecallExecutor) for the new surface.



# ── Task history pruning ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_task_replaces_prompt_with_compact_marker(tmp_path):
    """After a task activation, the verbose prompt is replaced with a compact marker."""
    from butterfly.core.types import AgentResult

    responses = [("Making progress", [])]
    provider = MockProvider(iter(responses))
    agent = Agent(system_prompt="sys", provider=provider)
    session = make_session(tmp_path, agent)

    # Write the recurring task card so a real task activation fires.
    past = (datetime.now() - timedelta(hours=2)).isoformat()
    save_card(session.tasks_dir, TaskCard(name="duty", description="- [ ] Do something", interval=600, start_at=past))
    (session.core_dir / "task.md").write_text(
        "Task wakeup.\n\nCurrent tasks:\n{task}", encoding="utf-8"
    )
    session._load_session_capabilities()

    await session.tick()

    # History should have 2 messages: compact marker + assistant response
    history = agent._history
    assert len(history) >= 2
    # First new message should be compact marker, not verbose prompt
    user_msgs = [m for m in history if m.role == "user"]
    assert user_msgs, "No user message in history"
    assert user_msgs[-1].content.startswith("[Task:"), (
        f"Expected compact marker, got: {user_msgs[-1].content!r}"
    )


@pytest.mark.asyncio
async def test_task_compact_marker_recognized_by_reshape_history(tmp_path):
    """_reshape_history drops compact [Task:...] markers like full prompts."""
    from butterfly.core.types import Message

    agent = Agent(system_prompt="sys", provider=MockProvider(iter([])))
    session = make_session(tmp_path, agent)

    # Simulate an orphaned compact marker at end of history
    agent._history = [Message(role="user", content="[Task:duty 2026-01-01T00:00:00]")]

    result = session._reshape_history("new user message")
    assert result == "new user message"
    assert len(agent._history) == 0  # orphan dropped


# ── memory layer truncation (_render_memory_layer) removed in v2.0.5 ─────────
# The inline-vs-truncated rendering logic for core/memory/*.md is gone.
# Sub-memories are now fetched on demand via the memory_recall tool.
