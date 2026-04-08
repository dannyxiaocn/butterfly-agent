"""Tests for curated built-in entity documentation."""
from __future__ import annotations

from pathlib import Path

from porter_test_support import repo_root_from

REPO_ROOT = repo_root_from(Path(__file__))
ENTITY_DIR = REPO_ROOT / "entity"
ACTIVE_ENTITIES = [
    "agent",
    "nutshell_dev",
    "nutshell_dev_codex",
]


def test_entity_catalog_exists():
    catalog = ENTITY_DIR / "README.md"
    assert catalog.exists()
    text = catalog.read_text(encoding="utf-8")
    assert "Entity Catalog" in text


def test_active_entities_are_listed_in_catalog():
    text = (ENTITY_DIR / "README.md").read_text(encoding="utf-8")
    for entity in ACTIVE_ENTITIES:
        assert f"`{entity}`" in text


def test_each_active_entity_has_readme():
    for entity in ACTIVE_ENTITIES:
        readme = ENTITY_DIR / entity / "README.md"
        assert readme.exists(), f"missing README for {entity}"
        text = readme.read_text(encoding="utf-8").strip()
        assert text.startswith(f"# {entity}")
        assert "## Purpose" in text


def test_entity_readmes_describe_status_or_notes():
    for entity in ACTIVE_ENTITIES:
        text = (ENTITY_DIR / entity / "README.md").read_text(encoding="utf-8")
        assert "## Notes" in text
