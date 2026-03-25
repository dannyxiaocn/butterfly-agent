import pytest

from ui.cli.new_agent import create_entity


def test_create_entity_with_missing_parent_fails_fast(tmp_path):
    with pytest.raises(ValueError, match="Parent entity 'missing' not found"):
        create_entity("child", tmp_path, "missing")


def test_create_entity_with_existing_parent_succeeds(tmp_path):
    parent_dir = tmp_path / "agent"
    parent_dir.mkdir(parents=True)
    (parent_dir / "agent.yaml").write_text("name: agent\n")

    created = create_entity("child", tmp_path, "agent")

    assert created == tmp_path / "child"
    manifest = (created / "agent.yaml").read_text()
    assert "extends: agent" in manifest


def test_create_entity_standalone_still_works_without_parent(tmp_path):
    created = create_entity("solo", tmp_path, None)

    assert created == tmp_path / "solo"
    manifest = (created / "agent.yaml").read_text()
    assert "extends:" not in manifest
    assert (created / "prompts" / "system.md").exists()
