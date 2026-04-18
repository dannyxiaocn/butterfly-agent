import pytest
import yaml

from ui.cli.new_agent import create_agent


def test_create_agent_with_missing_source_fails_fast(tmp_path):
    with pytest.raises(ValueError, match="Source agent 'missing' not found"):
        create_agent("child", tmp_path, "missing")


def test_create_agent_with_init_from_copies_and_updates_name(tmp_path):
    src_dir = tmp_path / "agent"
    src_dir.mkdir(parents=True)
    # Source may still have legacy `name:` (pre-v2.0.19 agenthub entries
    # written before the rename); create_agent must drop it and emit the
    # v2.0.19 `agent:` key so the resulting scaffold is single-schema.
    (src_dir / "config.yaml").write_text("name: agent\nmodel: gpt-4\n", encoding="utf-8")
    (src_dir / "prompts").mkdir()
    (src_dir / "prompts" / "system.md").write_text("sys prompt", encoding="utf-8")

    created = create_agent("child", tmp_path, "agent")

    assert created == tmp_path / "child"
    manifest = yaml.safe_load((created / "config.yaml").read_text())
    assert manifest["agent"] == "child"
    assert "name" not in manifest
    assert manifest["init_from"] == "agent"
    assert "extends" not in manifest
    # Prompt file should be copied
    assert (created / "prompts" / "system.md").read_text() == "sys prompt"


def test_create_agent_blank_creates_empty_files(tmp_path):
    created = create_agent("solo", tmp_path, None)

    assert created == tmp_path / "solo"
    manifest = (created / "config.yaml").read_text()
    assert "extends:" not in manifest
    assert "init_from:" not in manifest
    assert (created / "prompts" / "system.md").exists()
    assert (created / "prompts" / "task.md").exists()
    assert (created / "prompts" / "env.md").exists()
