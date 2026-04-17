"""Tests for curated built-in agent documentation (migrated to docs/)."""
from __future__ import annotations

from pathlib import Path


from conftest import REPO_ROOT
DOCS_DIR = REPO_ROOT / "docs" / "agent"
ACTIVE_AGENTS = [
    "agent",
    "butterfly_dev",
]


def test_agent_docs_exist():
    assert DOCS_DIR.exists()
    design = DOCS_DIR / "design.md"
    assert design.exists()
    text = design.read_text(encoding="utf-8")
    assert "Agent" in text


def test_active_agents_have_docs():
    for agent in ACTIVE_AGENTS:
        agent_docs = DOCS_DIR / agent
        assert agent_docs.exists(), f"missing docs dir for {agent}"
        assert (agent_docs / "design.md").exists(), f"missing design.md for {agent}"
        assert (agent_docs / "impl.md").exists(), f"missing impl.md for {agent}"
        assert (agent_docs / "todo.md").exists(), f"missing todo.md for {agent}"


def test_agent_design_has_content():
    for agent in ACTIVE_AGENTS:
        text = (DOCS_DIR / agent / "design.md").read_text(encoding="utf-8")
        assert len(text.strip()) > 0, f"empty design.md for {agent}"
