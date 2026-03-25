"""Tests for Session._load_session_capabilities:
- Agent-created (session-scoped) tools in sessions/<id>/core/tools/
- Session skills in sessions/<id>/core/skills/
- Memory injection from sessions/<id>/core/memory.md
- Tool-provider switching via params.json tool_providers
- No duplication on repeated capability reloads
"""
import json
import pytest
from pathlib import Path

from nutshell.core.provider import Provider
from nutshell.core.agent import Agent
from nutshell.core.skill import Skill
from nutshell.core.tool import tool
from nutshell.runtime.params import write_session_params
from nutshell.runtime.session import Session


class MockProvider(Provider):
    def __init__(self, responses):
        self._responses = iter(responses)

    async def complete(self, messages, tools, system_prompt, model, *, on_text_chunk=None, cache_system_prefix="", cache_last_human_turn=False):
        return next(self._responses)


def make_session(tmp_path: Path, agent: Agent, session_id: str = "test") -> Session:
    system_base = tmp_path / "_sessions"
    session = Session(agent, session_id=session_id, base_dir=tmp_path, system_base=system_base)
    # Pre-populate core/ prompt files that _load_session_capabilities reads
    (session.core_dir / "system.md").write_text(agent.system_prompt or "", encoding="utf-8")
    (session.core_dir / "heartbeat.md").write_text(
        getattr(agent, "heartbeat_prompt", "") or "", encoding="utf-8"
    )
    (session.core_dir / "session.md").write_text(
        getattr(agent, "session_context_template", "") or "", encoding="utf-8"
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
    @tool(description="entity tool")
    def entity_tool() -> str:
        return "entity"

    agent = Agent(system_prompt="Base", tools=[entity_tool], provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    write_tool_files(session.core_dir / "tools", "my_new_tool")
    session._load_session_capabilities()

    names = {t.name for t in agent.tools}
    assert "my_new_tool" in names


def test_session_tool_overrides_entity_tool(tmp_path):
    """Session tool with same name replaces the original tool."""
    @tool(description="original description")
    def my_tool() -> str:
        return "original"

    agent = Agent(system_prompt="Base", tools=[my_tool], provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    # First load to populate core/tools/ with original
    session._load_session_capabilities()

    # Now override with a new description
    write_tool_files(session.core_dir / "tools", "my_tool", description="overridden description")
    session._load_session_capabilities()

    matching = [t for t in agent.tools if t.name == "my_tool"]
    assert len(matching) == 1
    assert matching[0].description == "overridden description"


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
    result = await loaded.execute(msg="hello nutshell")
    assert "hello nutshell" in result


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


# ── Memory injection ──────────────────────────────────────────────────────────

def test_memory_injected_into_system_prompt(tmp_path):
    """Content of memory.md is injected into the assembled prompt each activation."""
    agent = Agent(system_prompt="Base system prompt.", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    session.memory_path.write_text("Remember: always be concise.")
    session._load_session_capabilities()

    full_prompt = agent._build_system_prompt()
    assert "Base system prompt." in full_prompt
    assert "Remember: always be concise." in full_prompt


def test_empty_memory_not_injected(tmp_path):
    """Empty memory.md does not append any extra block."""
    agent = Agent(system_prompt="Base.", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    session.memory_path.write_text("")
    session._load_session_capabilities()

    assert "Session Memory" not in agent._build_system_prompt()


def test_memory_cleared_removes_block(tmp_path):
    """After clearing memory.md the block is gone on next build."""
    agent = Agent(system_prompt="Base.", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    session.memory_path.write_text("Some memory.")
    session._load_session_capabilities()
    assert "Some memory." in agent._build_system_prompt()

    session.memory_path.write_text("")
    session._load_session_capabilities()
    assert "Some memory." not in agent._build_system_prompt()


def test_memory_updated_reflects_on_next_load(tmp_path):
    """After updating memory.md the new content appears on next build."""
    agent = Agent(system_prompt="Base.", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    session.memory_path.write_text("First memory.")
    session._load_session_capabilities()
    assert "First memory." in agent._build_system_prompt()

    session.memory_path.write_text("Second memory.")
    session._load_session_capabilities()
    assert "Second memory." in agent._build_system_prompt()
    assert "First memory." not in agent._build_system_prompt()


# ── Layered memory (core/memory/) ────────────────────────────────────────────

def test_memory_layer_dir_loaded(tmp_path):
    """Files in core/memory/ are loaded as extra labeled layers."""
    agent = Agent(system_prompt="Base.", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    memory_dir = session.core_dir / "memory"
    memory_dir.mkdir()
    (memory_dir / "facts.md").write_text("fact one")
    session._load_session_capabilities()

    full_prompt = agent._build_system_prompt()
    assert "## Memory: facts" in full_prompt
    assert "fact one" in full_prompt


def test_memory_layers_sorted_order(tmp_path):
    """core/memory/ files are loaded in sorted (alphabetical) filename order."""
    agent = Agent(system_prompt="Base.", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    memory_dir = session.core_dir / "memory"
    memory_dir.mkdir()
    (memory_dir / "beta.md").write_text("beta content")
    (memory_dir / "alpha.md").write_text("alpha content")
    (memory_dir / "gamma.md").write_text("gamma content")
    session._load_session_capabilities()

    names = [name for name, _ in agent.memory_layers]
    assert names == ["alpha", "beta", "gamma"]


def test_memory_layers_empty_files_skipped(tmp_path):
    """Empty files in core/memory/ are not included as layers."""
    agent = Agent(system_prompt="Base.", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    memory_dir = session.core_dir / "memory"
    memory_dir.mkdir()
    (memory_dir / "empty.md").write_text("")
    (memory_dir / "nonempty.md").write_text("some content")
    session._load_session_capabilities()

    names = [name for name, _ in agent.memory_layers]
    assert "empty" not in names
    assert "nonempty" in names


def test_memory_primary_and_layers_both_rendered(tmp_path):
    """Primary memory.md and core/memory/ layers are both present in the prompt."""
    agent = Agent(system_prompt="Base.", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    session.memory_path.write_text("primary content")
    memory_dir = session.core_dir / "memory"
    memory_dir.mkdir()
    (memory_dir / "extra.md").write_text("extra content")
    session._load_session_capabilities()

    full_prompt = agent._build_system_prompt()
    assert "## Session Memory" in full_prompt
    assert "primary content" in full_prompt
    assert "## Memory: extra" in full_prompt
    assert "extra content" in full_prompt


def test_memory_only_layers_no_primary(tmp_path):
    """core/memory/ layers work correctly when memory.md is empty."""
    agent = Agent(system_prompt="Base.", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    # memory.md is empty (default)
    memory_dir = session.core_dir / "memory"
    memory_dir.mkdir()
    (memory_dir / "notes.md").write_text("layer note")
    session._load_session_capabilities()

    full_prompt = agent._build_system_prompt()
    assert "## Memory: notes" in full_prompt
    assert "layer note" in full_prompt
    assert "## Session Memory" not in full_prompt


def test_no_memory_dir_backward_compat(tmp_path):
    """When core/memory/ does not exist, behavior is identical to before."""
    agent = Agent(system_prompt="Base.", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    session.memory_path.write_text("legacy memory")
    session._load_session_capabilities()

    full_prompt = agent._build_system_prompt()
    assert "## Session Memory\n\nlegacy memory" in full_prompt
    assert agent.memory_layers == []


def test_memory_layers_removed_on_next_reload(tmp_path):
    """Removing core/memory/ files clears stale layers on the next capability reload."""
    agent = Agent(system_prompt="Base.", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    memory_dir = session.core_dir / "memory"
    memory_dir.mkdir()
    layer_path = memory_dir / "facts.md"
    layer_path.write_text("fact one")
    session._load_session_capabilities()
    assert agent.memory_layers == [("facts", "fact one")]

    layer_path.unlink()
    session._load_session_capabilities()

    assert agent.memory_layers == []
    full_prompt = agent._build_system_prompt()
    assert "## Memory: facts" not in full_prompt
    assert "fact one" not in full_prompt


# ── Tool-provider override ────────────────────────────────────────────────────

def test_tool_provider_override_switches_impl(tmp_path, monkeypatch):
    """params.json tool_providers field replaces the tool's implementation callable."""
    import nutshell.tool_engine.registry as _registry

    call_log: list[str] = []

    async def fake_tavily(**kwargs) -> str:
        call_log.append("tavily")
        return "tavily result"

    monkeypatch.setattr(
        _registry,
        "resolve_tool_impl",
        lambda tool_name, provider_name: fake_tavily if (tool_name == "web_search" and provider_name == "tavily") else None,
    )

    # Create a web_search tool JSON in core/tools/
    agent = Agent(system_prompt="Base", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    tool_schema = {
        "name": "web_search",
        "description": "web search",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
    (session.core_dir / "tools" / "web_search.json").write_text(json.dumps(tool_schema))

    write_session_params(session.session_dir, tool_providers={"web_search": "tavily"})
    session._load_session_capabilities()

    ws = next(t for t in agent.tools if t.name == "web_search")
    import asyncio
    result = asyncio.get_event_loop().run_until_complete(ws.execute(query="test"))
    assert result == "tavily result"
    assert "brave" not in call_log


def test_tool_provider_unknown_keeps_original_impl(tmp_path, monkeypatch):
    """If resolve_tool_impl returns None for unknown provider, original shell impl is kept."""
    import nutshell.tool_engine.registry as _registry

    monkeypatch.setattr(
        _registry,
        "resolve_tool_impl",
        lambda tool_name, provider_name: None,
    )

    agent = Agent(system_prompt="Base", provider=MockProvider([]))
    session = make_session(tmp_path, agent)

    tool_schema = {
        "name": "custom_search",
        "description": "custom search",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
    (session.core_dir / "tools" / "custom_search.json").write_text(json.dumps(tool_schema))
    # Create a shell impl that returns a known string
    sh = session.core_dir / "tools" / "custom_search.sh"
    sh.write_text('#!/bin/bash\necho "shell result"')
    sh.chmod(0o755)

    write_session_params(session.session_dir, tool_providers={"custom_search": "nonexistent"})
    session._load_session_capabilities()

    ws = next(t for t in agent.tools if t.name == "custom_search")
    import asyncio
    result = asyncio.get_event_loop().run_until_complete(ws.execute(query="test"))
    assert "shell result" in result
