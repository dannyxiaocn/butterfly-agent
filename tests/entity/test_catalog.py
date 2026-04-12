"""Tests for curated built-in entity documentation (migrated to docs/)."""
from __future__ import annotations

from pathlib import Path


from conftest import REPO_ROOT
DOCS_DIR = REPO_ROOT / "docs" / "entity"
ACTIVE_ENTITIES = [
    "agent",
    "nutshell_dev",
    "nutshell_dev_codex",
]


def test_entity_docs_exist():
    assert DOCS_DIR.exists()
    design = DOCS_DIR / "design.md"
    assert design.exists()
    text = design.read_text(encoding="utf-8")
    assert "Entity" in text


def test_active_entities_have_docs():
    for entity in ACTIVE_ENTITIES:
        entity_docs = DOCS_DIR / entity
        assert entity_docs.exists(), f"missing docs dir for {entity}"
        assert (entity_docs / "design.md").exists(), f"missing design.md for {entity}"
        assert (entity_docs / "impl.md").exists(), f"missing impl.md for {entity}"
        assert (entity_docs / "todo.md").exists(), f"missing todo.md for {entity}"


def test_entity_design_has_content():
    for entity in ACTIVE_ENTITIES:
        text = (DOCS_DIR / entity / "design.md").read_text(encoding="utf-8")
        assert len(text.strip()) > 0, f"empty design.md for {entity}"
