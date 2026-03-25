"""Tests for AgentLoader deep entity inheritance (A → B → C).

Rules under test:
- prompts: null → inherit parent's resolved value; string → load from this entity's dir.
- tools/skills: null → inherit parent's resolved list; [] → explicitly empty; [list] → child-first.
- model/provider: null → inherit from parent; fallback to built-in defaults.
- Three levels (A→B→C): C wins over B wins over A for any explicitly set field.
"""
import json
import pytest
import yaml
from pathlib import Path

from nutshell.llm_engine.loader import AgentLoader


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_entity(base: Path, name: str, **fields) -> Path:
    """Write a minimal agent.yaml for an entity and return its directory."""
    entity_dir = base / name
    entity_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"name": name, **fields}
    (entity_dir / "agent.yaml").write_text(yaml.dump(manifest, default_flow_style=False))
    return entity_dir


def write_prompt_file(entity_dir: Path, rel: str, content: str) -> None:
    p = entity_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def write_tool_json(entity_dir: Path, name: str, description: str = "") -> None:
    tools_dir = entity_dir / "tools"
    tools_dir.mkdir(exist_ok=True)
    (tools_dir / f"{name}.json").write_text(json.dumps({
        "name": name,
        "description": description or name,
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }))


def write_skill_dir(entity_dir: Path, name: str, description: str = "") -> None:
    skill_dir = entity_dir / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description or name}\n---\n\n{name} body.\n"
    )


def load(base: Path, name: str) -> object:
    """Load an agent from entity dir, skipping real provider resolution gracefully."""
    return AgentLoader().load(base / name)


# ── System-prompt inheritance ─────────────────────────────────────────────────

def test_three_level_inherits_prompt_from_root(tmp_path):
    """C(null)→B(null)→A: C gets A's system prompt."""
    a = make_entity(tmp_path, "a", model="claude-sonnet-4-6", provider="anthropic",
                    prompts={"system": "prompts/system.md"}, tools=None, skills=None)
    write_prompt_file(a, "prompts/system.md", "System from A")

    make_entity(tmp_path, "b", extends="a", model=None, provider=None,
                prompts={"system": None}, tools=None, skills=None)
    make_entity(tmp_path, "c", extends="b", model=None, provider=None,
                prompts={"system": None}, tools=None, skills=None)

    agent = load(tmp_path, "c")
    assert agent.system_prompt == "System from A"


def test_b_overrides_prompt_c_null_gets_b(tmp_path):
    """C(null)→B(override)→A: C gets B's prompt, not A's."""
    a = make_entity(tmp_path, "a", model="claude-sonnet-4-6", provider="anthropic",
                    prompts={"system": "prompts/system.md"}, tools=None, skills=None)
    write_prompt_file(a, "prompts/system.md", "System from A")

    b = make_entity(tmp_path, "b", extends="a", model=None, provider=None,
                    prompts={"system": "prompts/system.md"}, tools=None, skills=None)
    write_prompt_file(b, "prompts/system.md", "System from B")

    make_entity(tmp_path, "c", extends="b", model=None, provider=None,
                prompts={"system": None}, tools=None, skills=None)

    agent = load(tmp_path, "c")
    assert agent.system_prompt == "System from B"


def test_c_overrides_prompt_wins_over_b_and_a(tmp_path):
    """C(override)→B(override)→A: C's prompt takes priority over all parents."""
    a = make_entity(tmp_path, "a", model="claude-sonnet-4-6", provider="anthropic",
                    prompts={"system": "prompts/system.md"}, tools=None, skills=None)
    write_prompt_file(a, "prompts/system.md", "System from A")

    b = make_entity(tmp_path, "b", extends="a", model=None, provider=None,
                    prompts={"system": "prompts/system.md"}, tools=None, skills=None)
    write_prompt_file(b, "prompts/system.md", "System from B")

    c = make_entity(tmp_path, "c", extends="b", model=None, provider=None,
                    prompts={"system": "prompts/system.md"}, tools=None, skills=None)
    write_prompt_file(c, "prompts/system.md", "System from C")

    agent = load(tmp_path, "c")
    assert agent.system_prompt == "System from C"


def test_b_and_c_each_override_different_prompts(tmp_path):
    """B overrides heartbeat, C overrides system — each keeps its own override."""
    a = make_entity(tmp_path, "a", model="claude-sonnet-4-6", provider="anthropic",
                    prompts={"system": "prompts/system.md", "heartbeat": "prompts/heartbeat.md"},
                    tools=None, skills=None)
    write_prompt_file(a, "prompts/system.md", "System from A")
    write_prompt_file(a, "prompts/heartbeat.md", "Heartbeat from A")

    b = make_entity(tmp_path, "b", extends="a", model=None, provider=None,
                    prompts={"system": None, "heartbeat": "prompts/heartbeat.md"},
                    tools=None, skills=None)
    write_prompt_file(b, "prompts/heartbeat.md", "Heartbeat from B")

    c = make_entity(tmp_path, "c", extends="b", model=None, provider=None,
                    prompts={"system": "prompts/system.md", "heartbeat": None},
                    tools=None, skills=None)
    write_prompt_file(c, "prompts/system.md", "System from C")

    agent = load(tmp_path, "c")
    assert agent.system_prompt == "System from C"
    assert agent.heartbeat_prompt == "Heartbeat from B"


# ── Tool inheritance ──────────────────────────────────────────────────────────

def test_tools_null_chain_inherits_from_root(tmp_path):
    """C(null)→B(null)→A: C inherits A's tools."""
    a = make_entity(tmp_path, "a", model="claude-sonnet-4-6", provider="anthropic",
                    prompts={}, tools=["tools/alpha.json"], skills=None)
    write_tool_json(a, "alpha", "alpha from A")

    make_entity(tmp_path, "b", extends="a", model=None, provider=None,
                prompts={}, tools=None, skills=None)
    make_entity(tmp_path, "c", extends="b", model=None, provider=None,
                prompts={}, tools=None, skills=None)

    agent = load(tmp_path, "c")
    assert any(t.name == "alpha" for t in agent.tools)


def test_tools_child_first_c_has_file_beats_b(tmp_path):
    """C's own alpha.json wins over B's for the same relative path."""
    a = make_entity(tmp_path, "a", model="claude-sonnet-4-6", provider="anthropic",
                    prompts={}, tools=["tools/alpha.json"], skills=None)
    write_tool_json(a, "alpha", "alpha from A")

    b = make_entity(tmp_path, "b", extends="a", model=None, provider=None,
                    prompts={}, tools=["tools/alpha.json"], skills=None)
    write_tool_json(b, "alpha", "alpha from B")

    c = make_entity(tmp_path, "c", extends="b", model=None, provider=None,
                    prompts={}, tools=["tools/alpha.json"], skills=None)
    write_tool_json(c, "alpha", "alpha from C")

    agent = load(tmp_path, "c")
    alpha = next(t for t in agent.tools if t.name == "alpha")
    assert alpha.description == "alpha from C"


def test_tools_child_first_c_has_no_file_falls_back_to_b(tmp_path):
    """When C has no alpha.json but B does, B's file is used (child-first fallback)."""
    a = make_entity(tmp_path, "a", model="claude-sonnet-4-6", provider="anthropic",
                    prompts={}, tools=["tools/alpha.json"], skills=None)
    write_tool_json(a, "alpha", "alpha from A")

    b = make_entity(tmp_path, "b", extends="a", model=None, provider=None,
                    prompts={}, tools=["tools/alpha.json"], skills=None)
    write_tool_json(b, "alpha", "alpha from B")

    # C references same path but has no local copy → resolves to B's
    make_entity(tmp_path, "c", extends="b", model=None, provider=None,
                prompts={}, tools=["tools/alpha.json"], skills=None)

    agent = load(tmp_path, "c")
    alpha = next(t for t in agent.tools if t.name == "alpha")
    assert alpha.description == "alpha from B"


def test_tools_explicit_empty_no_inheritance(tmp_path):
    """C with tools: [] gets no tools, even though A and B have them."""
    a = make_entity(tmp_path, "a", model="claude-sonnet-4-6", provider="anthropic",
                    prompts={}, tools=["tools/alpha.json"], skills=None)
    write_tool_json(a, "alpha")

    make_entity(tmp_path, "b", extends="a", model=None, provider=None,
                prompts={}, tools=None, skills=None)

    make_entity(tmp_path, "c", extends="b", model=None, provider=None,
                prompts={}, tools=[], skills=None)

    agent = load(tmp_path, "c")
    assert agent.tools == []


def test_tools_b_adds_new_tool_c_inherits_it(tmp_path):
    """B adds beta tool (not in A); C (null) inherits both alpha and beta."""
    a = make_entity(tmp_path, "a", model="claude-sonnet-4-6", provider="anthropic",
                    prompts={}, tools=["tools/alpha.json"], skills=None)
    write_tool_json(a, "alpha")

    b = make_entity(tmp_path, "b", extends="a", model=None, provider=None,
                    prompts={}, tools=["tools/alpha.json", "tools/beta.json"], skills=None)
    write_tool_json(b, "beta", "beta from B")

    make_entity(tmp_path, "c", extends="b", model=None, provider=None,
                prompts={}, tools=None, skills=None)

    agent = load(tmp_path, "c")
    names = {t.name for t in agent.tools}
    assert "alpha" in names
    assert "beta" in names


# ── Skill inheritance ─────────────────────────────────────────────────────────

def test_skills_null_chain_inherits_from_root(tmp_path):
    """C(null)→B(null)→A: C inherits A's skills."""
    a = make_entity(tmp_path, "a", model="claude-sonnet-4-6", provider="anthropic",
                    prompts={}, tools=None, skills=["skills/alpha_skill"])
    write_skill_dir(a, "alpha_skill", "alpha from A")

    make_entity(tmp_path, "b", extends="a", model=None, provider=None,
                prompts={}, tools=None, skills=None)
    make_entity(tmp_path, "c", extends="b", model=None, provider=None,
                prompts={}, tools=None, skills=None)

    agent = load(tmp_path, "c")
    assert any(s.name == "alpha_skill" for s in agent.skills)


def test_skills_explicit_empty_no_inheritance(tmp_path):
    """C with skills: [] gets no skills, even though A has them."""
    a = make_entity(tmp_path, "a", model="claude-sonnet-4-6", provider="anthropic",
                    prompts={}, tools=None, skills=["skills/alpha_skill"])
    write_skill_dir(a, "alpha_skill")

    make_entity(tmp_path, "b", extends="a", model=None, provider=None,
                prompts={}, tools=None, skills=None)

    make_entity(tmp_path, "c", extends="b", model=None, provider=None,
                prompts={}, tools=None, skills=[])

    agent = load(tmp_path, "c")
    assert agent.skills == []


def test_skills_child_first_c_has_skill_beats_b(tmp_path):
    """C's local SKILL.md for alpha_skill is used over B's (child-first)."""
    a = make_entity(tmp_path, "a", model="claude-sonnet-4-6", provider="anthropic",
                    prompts={}, tools=None, skills=["skills/alpha_skill"])
    write_skill_dir(a, "alpha_skill", "alpha from A")

    b = make_entity(tmp_path, "b", extends="a", model=None, provider=None,
                    prompts={}, tools=None, skills=["skills/alpha_skill"])
    write_skill_dir(b, "alpha_skill", "alpha from B")

    c = make_entity(tmp_path, "c", extends="b", model=None, provider=None,
                    prompts={}, tools=None, skills=["skills/alpha_skill"])
    write_skill_dir(c, "alpha_skill", "alpha from C")

    agent = load(tmp_path, "c")
    skill = next(s for s in agent.skills if s.name == "alpha_skill")
    assert skill.description == "alpha from C"


def test_skills_child_first_c_no_file_falls_back_to_b(tmp_path):
    """C references alpha_skill but has no local copy; B's file is used."""
    a = make_entity(tmp_path, "a", model="claude-sonnet-4-6", provider="anthropic",
                    prompts={}, tools=None, skills=["skills/alpha_skill"])
    write_skill_dir(a, "alpha_skill", "alpha from A")

    b = make_entity(tmp_path, "b", extends="a", model=None, provider=None,
                    prompts={}, tools=None, skills=["skills/alpha_skill"])
    write_skill_dir(b, "alpha_skill", "alpha from B")

    # C references same path, no local copy → resolves to B's
    make_entity(tmp_path, "c", extends="b", model=None, provider=None,
                prompts={}, tools=None, skills=["skills/alpha_skill"])

    agent = load(tmp_path, "c")
    skill = next(s for s in agent.skills if s.name == "alpha_skill")
    assert skill.description == "alpha from B"


def test_c_adds_new_skill_not_in_parents(tmp_path):
    """C's explicit skill list adds a new skill that doesn't exist in A or B."""
    a = make_entity(tmp_path, "a", model="claude-sonnet-4-6", provider="anthropic",
                    prompts={}, tools=None, skills=["skills/alpha_skill"])
    write_skill_dir(a, "alpha_skill")

    make_entity(tmp_path, "b", extends="a", model=None, provider=None,
                prompts={}, tools=None, skills=None)

    c = make_entity(tmp_path, "c", extends="b", model=None, provider=None,
                    prompts={}, tools=None,
                    skills=["skills/alpha_skill", "skills/new_skill"])
    write_skill_dir(c, "new_skill", "new in C")

    agent = load(tmp_path, "c")
    names = {s.name for s in agent.skills}
    assert "alpha_skill" in names
    assert "new_skill" in names


# ── Model / provider inheritance ─────────────────────────────────────────────

def test_model_propagates_through_chain(tmp_path):
    """C(null)→B(null)→A: C gets A's model."""
    make_entity(tmp_path, "a", model="claude-opus-4-6", provider="anthropic",
                prompts={}, tools=None, skills=None)
    make_entity(tmp_path, "b", extends="a", model=None, provider=None,
                prompts={}, tools=None, skills=None)
    make_entity(tmp_path, "c", extends="b", model=None, provider=None,
                prompts={}, tools=None, skills=None)

    agent = load(tmp_path, "c")
    assert agent.model == "claude-opus-4-6"


def test_b_overrides_model_c_inherits_b_not_a(tmp_path):
    """B overrides model; C (null) gets B's model, not A's."""
    make_entity(tmp_path, "a", model="claude-opus-4-6", provider="anthropic",
                prompts={}, tools=None, skills=None)
    make_entity(tmp_path, "b", extends="a", model="kimi-for-coding", provider=None,
                prompts={}, tools=None, skills=None)
    make_entity(tmp_path, "c", extends="b", model=None, provider=None,
                prompts={}, tools=None, skills=None)

    agent = load(tmp_path, "c")
    assert agent.model == "kimi-for-coding"


def test_c_model_wins_over_b_and_a(tmp_path):
    """C's explicit model overrides both B's and A's."""
    make_entity(tmp_path, "a", model="claude-opus-4-6", provider="anthropic",
                prompts={}, tools=None, skills=None)
    make_entity(tmp_path, "b", extends="a", model="kimi-for-coding", provider=None,
                prompts={}, tools=None, skills=None)
    make_entity(tmp_path, "c", extends="b", model="claude-sonnet-4-6", provider=None,
                prompts={}, tools=None, skills=None)

    agent = load(tmp_path, "c")
    assert agent.model == "claude-sonnet-4-6"


# ── Real entity chain ─────────────────────────────────────────────────────────

def test_real_nutshell_dev_entity_chain():
    """nutshell_dev → kimi_agent → agent inheritance loads without errors."""
    entity_root = Path(__file__).parent.parent / "entity"
    if not (entity_root / "nutshell_dev" / "agent.yaml").exists():
        pytest.skip("nutshell_dev entity not found")

    agent = AgentLoader().load(entity_root / "nutshell_dev")

    # kimi_agent sets the model; nutshell_dev inherits it
    assert agent.model  # non-empty
    # agent provides tools; nutshell_dev inherits them through kimi_agent
    assert len(agent.tools) > 0
    # nutshell_dev explicitly lists skills including nutshell
    skill_names = {s.name for s in agent.skills}
    assert "nutshell" in skill_names


def test_real_kimi_agent_inherits_tools_from_agent():
    """kimi_agent (tools: null) inherits agent's tools."""
    entity_root = Path(__file__).parent.parent / "entity"
    if not (entity_root / "kimi_agent" / "agent.yaml").exists():
        pytest.skip("kimi_agent entity not found")

    agent = AgentLoader().load(entity_root / "kimi_agent")

    assert len(agent.tools) > 0
    names = {t.name for t in agent.tools}
    assert "bash" in names


def test_agent_entity_loads_all_builtin_tools():
    """agent entity must include all built-in tools so sessions have full capability."""
    entity_root = Path(__file__).parent.parent / "entity"
    agent = AgentLoader().load(entity_root / "agent")
    names = {t.name for t in agent.tools}

    expected = {
        "bash",
        "web_search",
        "send_to_session",
        "spawn_session",
        "propose_entity_update",
        "fetch_url",
        "recall_memory",
        "state_diff",
        "git_checkpoint",
    }
    missing = expected - names
    assert not missing, f"Missing tools from agent entity: {missing}"
