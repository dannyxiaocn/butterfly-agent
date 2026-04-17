"""init_session — parent_session_id + mode + initial_message_id support.

These extend the v2.0.8 manifest-last invariant covered elsewhere; here we
focus on the new fields the sub_agent tool relies on.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from butterfly.session_engine.session_init import init_session


def _agent_dir(tmp_path: Path) -> Path:
    """Build a minimal agent tree: just enough for init_session to copy."""
    base = tmp_path / "agenthub"
    ag = base / "agent"
    ag.mkdir(parents=True)
    (ag / "config.yaml").write_text("name: agent\nmodel: test-model\nprovider: anthropic\n", encoding="utf-8")
    (ag / "system.md").write_text("you are agent", encoding="utf-8")
    (ag / "task.md").write_text("", encoding="utf-8")
    (ag / "env.md").write_text("env: {session_id}", encoding="utf-8")
    (ag / "tools.md").write_text("read\n", encoding="utf-8")
    return base


def _bases(tmp_path: Path) -> tuple[Path, Path, Path]:
    sessions_base = tmp_path / "sessions"
    sys_base = tmp_path / "_sessions"
    agent_base = _agent_dir(tmp_path)
    sessions_base.mkdir()
    sys_base.mkdir()
    return sessions_base, sys_base, agent_base


def test_manifest_records_parent_and_mode(tmp_path: Path) -> None:
    sessions_base, sys_base, agent_base = _bases(tmp_path)
    init_session(
        "child-1", "agent",
        sessions_base=sessions_base, system_sessions_base=sys_base, agent_base=agent_base,
        parent_session_id="parent-x",
        mode="explorer",
    )
    manifest = json.loads((sys_base / "child-1" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["parent_session_id"] == "parent-x"
    assert manifest["mode"] == "explorer"
    assert manifest["agent"] == "agent"


def test_top_level_session_omits_parent_and_mode(tmp_path: Path) -> None:
    sessions_base, sys_base, agent_base = _bases(tmp_path)
    init_session(
        "top", "agent",
        sessions_base=sessions_base, system_sessions_base=sys_base, agent_base=agent_base,
    )
    manifest = json.loads((sys_base / "top" / "manifest.json").read_text(encoding="utf-8"))
    assert "parent_session_id" not in manifest
    assert "mode" not in manifest


def test_invalid_mode_raises(tmp_path: Path) -> None:
    sessions_base, sys_base, agent_base = _bases(tmp_path)
    with pytest.raises(ValueError, match="invalid mode"):
        init_session(
            "bad", "agent",
            sessions_base=sessions_base, system_sessions_base=sys_base, agent_base=agent_base,
            mode="overlord",
        )


def test_initial_message_id_persists_on_user_input(tmp_path: Path) -> None:
    sessions_base, sys_base, agent_base = _bases(tmp_path)
    init_session(
        "with-msg", "agent",
        sessions_base=sessions_base, system_sessions_base=sys_base, agent_base=agent_base,
        initial_message="hello",
        initial_message_id="msg-fixed-123",
    )
    ctx = (sys_base / "with-msg" / "context.jsonl").read_text(encoding="utf-8").strip()
    event = json.loads(ctx)
    assert event["type"] == "user_input"
    assert event["content"] == "hello"
    assert event["id"] == "msg-fixed-123"


def test_explorer_mode_copies_mode_md_from_toolhub(tmp_path: Path) -> None:
    """``toolhub/sub_agent/explorer.md`` ends up at
    ``sessions/<id>/core/mode.md``. Since v2.0.13 (post PR #28 review)
    init_session hard-fails when the prompt is missing rather than
    silently recording an inconsistent manifest — that behaviour is
    covered by :func:`test_missing_mode_prompt_raises` below.
    """
    sessions_base, sys_base, agent_base = _bases(tmp_path)
    init_session(
        "explorer-child", "agent",
        sessions_base=sessions_base, system_sessions_base=sys_base, agent_base=agent_base,
        mode="explorer",
    )
    mode_md = sessions_base / "explorer-child" / "core" / "mode.md"
    assert mode_md.exists(), "Expected mode.md to be copied from toolhub"
    body = mode_md.read_text(encoding="utf-8")
    assert "Explorer Mode" in body
    assert "playground" in body.lower()


def test_missing_mode_prompt_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the toolhub prompt file doesn't exist, init_session refuses to
    record ``mode`` rather than leaving a mode-aware child without its
    agent-visible rule set. (Cubic review, PR #28.)"""
    sessions_base, sys_base, agent_base = _bases(tmp_path)
    # Point the toolhub lookup at an empty dir so explorer.md is missing.
    empty_toolhub = tmp_path / "fake_toolhub"
    (empty_toolhub / "sub_agent").mkdir(parents=True)
    import butterfly.session_engine.session_init as mod
    monkeypatch.setattr(mod, "_TOOLHUB_DIR", empty_toolhub)
    with pytest.raises(FileNotFoundError, match="inconsistent state"):
        init_session(
            "child-no-prompt", "agent",
            sessions_base=sessions_base, system_sessions_base=sys_base, agent_base=agent_base,
            mode="explorer",
        )
