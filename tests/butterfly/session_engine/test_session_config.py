"""Tests for butterfly.session_engine.session_config module."""
from __future__ import annotations

import json
import pytest
from pathlib import Path

from butterfly.session_engine.session_config import (
    DEFAULT_CONFIG,
    config_path,
    read_config,
    write_config,
    ensure_config,
)


# ── config_path ──────────────────────────────────────────────────────────────


def test_config_path_in_session_dir(tmp_path):
    """config_path returns core/config.yaml for session dirs."""
    core = tmp_path / "core"
    core.mkdir()
    assert config_path(tmp_path) == core / "config.yaml"


def test_config_path_in_agent_dir(tmp_path):
    """config_path returns config.yaml for agent dirs (no core/)."""
    assert config_path(tmp_path) == tmp_path / "config.yaml"


# ── read_config ──────────────────────────────────────────────────────────────


def test_read_config_returns_defaults_when_missing(tmp_path):
    """read_config returns DEFAULT_CONFIG when no config file exists."""
    cfg = read_config(tmp_path)
    assert cfg == DEFAULT_CONFIG


def test_read_config_reads_yaml(tmp_path):
    """read_config reads config.yaml and merges with defaults."""
    import yaml
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"model": "gpt-4", "thinking": True}), encoding="utf-8")
    cfg = read_config(tmp_path)
    assert cfg["model"] == "gpt-4"
    assert cfg["thinking"] is True
    # Defaults still present
    assert "prompts" in cfg
    assert cfg["duty"] is None



def test_read_config_corrupt_yaml_returns_defaults(tmp_path):
    """read_config returns defaults when config.yaml is corrupt."""
    path = tmp_path / "config.yaml"
    path.write_text("{{{bad yaml", encoding="utf-8")
    cfg = read_config(tmp_path)
    # Should still have defaults
    assert cfg["duty"] is None



# ── write_config ─────────────────────────────────────────────────────────────


def test_write_config_creates_file(tmp_path):
    """write_config creates config.yaml with merged values."""
    write_config(tmp_path, model="gpt-4", thinking=True)
    cfg = read_config(tmp_path)
    assert cfg["model"] == "gpt-4"
    assert cfg["thinking"] is True


def test_write_config_merges_updates(tmp_path):
    """write_config merges updates into existing config."""
    write_config(tmp_path, model="gpt-4")
    write_config(tmp_path, thinking=True)
    cfg = read_config(tmp_path)
    assert cfg["model"] == "gpt-4"
    assert cfg["thinking"] is True


# ── ensure_config ────────────────────────────────────────────────────────────


def test_ensure_config_creates_when_absent(tmp_path):
    """ensure_config creates config.yaml with defaults when absent."""
    ensure_config(tmp_path)
    assert (tmp_path / "config.yaml").exists()
    cfg = read_config(tmp_path)
    assert cfg["duty"] is None


def test_ensure_config_noop_when_exists(tmp_path):
    """ensure_config does not overwrite existing config.yaml."""
    write_config(tmp_path, model="custom")
    ensure_config(tmp_path, model="default")
    cfg = read_config(tmp_path)
    assert cfg["model"] == "custom"


def test_ensure_config_accepts_custom_defaults(tmp_path):
    """ensure_config applies custom defaults on first creation."""
    ensure_config(tmp_path, model="gpt-4", thinking=True)
    cfg = read_config(tmp_path)
    assert cfg["model"] == "gpt-4"
    assert cfg["thinking"] is True


# ── DEFAULT_CONFIG shape ─────────────────────────────────────────────────────


def test_default_config_has_expected_keys():
    """DEFAULT_CONFIG has all expected keys."""
    expected = {
        "agent", "description", "model", "provider",
        "fallback_model", "fallback_provider",
        "max_iterations", "thinking", "thinking_budget", "thinking_effort",
        "prompts", "tools", "skills", "duty",
    }
    assert set(DEFAULT_CONFIG.keys()) == expected


def test_default_config_prompts_structure():
    """DEFAULT_CONFIG prompts has system, task, env."""
    prompts = DEFAULT_CONFIG["prompts"]
    assert prompts["system"] == "system.md"
    assert prompts["task"] == "task.md"
    assert prompts["env"] == "env.md"
