"""Sub-agent recursion depth cap (PR #28 review nit).

Prevents a runaway executor → sub_agent → executor → sub_agent chain.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from butterfly.tool_engine.sub_agent import (
    _MAX_SUB_AGENT_DEPTH,
    _spawn_child,
)


def _make_parent_manifest(sys_base: Path, sid: str, depth: int | None = None) -> None:
    d = sys_base / sid
    d.mkdir(parents=True)
    payload = {
        "session_id": sid, "agent": "agent",
        "created_at": datetime.now().isoformat(),
    }
    if depth is not None:
        payload["sub_agent_depth"] = depth
    (d / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def _agent_base(tmp_path: Path) -> Path:
    base = tmp_path / "agenthub"
    ag = base / "agent"
    ag.mkdir(parents=True)
    (ag / "config.yaml").write_text("name: agent\nmodel: test\nprovider: anthropic\n", encoding="utf-8")
    (ag / "system.md").write_text("", encoding="utf-8")
    (ag / "task.md").write_text("", encoding="utf-8")
    (ag / "env.md").write_text("", encoding="utf-8")
    (ag / "tools.md").write_text("", encoding="utf-8")
    return base


def test_spawn_increments_depth_in_child_manifest(tmp_path: Path) -> None:
    sessions_base = tmp_path / "sessions"
    sys_base = tmp_path / "_sessions"
    sessions_base.mkdir()
    sys_base.mkdir()
    agent_base = _agent_base(tmp_path)

    _make_parent_manifest(sys_base, "top-parent")  # depth=0 (omitted)
    child_id, _msg, _agt = _spawn_child(
        parent_session_id="top-parent", mode="explorer", task="x", name="probe",
        sessions_base=sessions_base,
        system_sessions_base=sys_base,
        agent_base=agent_base,
    )
    child_manifest = json.loads((sys_base / child_id / "manifest.json").read_text(encoding="utf-8"))
    assert child_manifest["sub_agent_depth"] == 1


def test_spawn_refuses_when_parent_at_max_depth(tmp_path: Path) -> None:
    sessions_base = tmp_path / "sessions"
    sys_base = tmp_path / "_sessions"
    sessions_base.mkdir()
    sys_base.mkdir()
    agent_base = _agent_base(tmp_path)

    _make_parent_manifest(sys_base, "deep-parent", depth=_MAX_SUB_AGENT_DEPTH)
    with pytest.raises(RuntimeError, match="depth"):
        _spawn_child(
            parent_session_id="deep-parent", mode="explorer", task="x", name="probe",
            sessions_base=sessions_base,
            system_sessions_base=sys_base,
            agent_base=agent_base,
        )
