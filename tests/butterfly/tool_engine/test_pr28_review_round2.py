"""PR #28 second-round review: Guardian wired through ALL shell paths.

The first round shipped a Guardian for inline ``bash`` / ``write`` / ``edit``
but missed two ways an explorer-mode child could still escape:

  - Bug #4: ``session_shell`` (persistent shell) ran with the session's
    own workdir, ignoring the Guardian boundary.
  - Bug #5: background-mode ``bash`` (``run_in_background=true``) routed
    through ``BashRunner`` which read ``input["workdir"]`` without
    consulting the Guardian.

Plus two UX gaps:

  - Gap #6: A sub-agent child had no easy hand-off to the parent's
    playground (the parent's working files were unreachable without an
    absolute-path read).
  - Gap #7: HUD ``⚙ N sub-agents running`` count was lost on page
    refresh because the SSE stream only re-broadcasts on state change.

These tests pin the post-fix invariants.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from butterfly.core.guardian import Guardian
from butterfly.session_engine.session_init import init_session
from butterfly.tool_engine.background import (
    BackgroundContext,
    BackgroundEvent,
    BackgroundTaskManager,
)


# ── Bug #5: BashRunner respects Guardian (background bash) ────────────────────


def _wait_terminal(mgr: BackgroundTaskManager, tid: str, timeout: float = 15.0):
    import time
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        try:
            evt = asyncio.get_event_loop().run_until_complete(
                asyncio.wait_for(mgr.events.get(), timeout=1.0)
            )
        except asyncio.TimeoutError:
            continue
        if evt.tid != tid:
            continue
        seen.append(evt)
        if evt.kind == "completed":
            return seen
    raise AssertionError(f"no completion for {tid}; saw {[e.kind for e in seen]}")


@pytest.mark.skipif(sys.platform == "win32", reason="needs bash")
@pytest.mark.asyncio
async def test_background_bash_with_guardian_pins_cwd_and_env(tmp_path: Path) -> None:
    panel_dir = tmp_path / "panel"
    out_dir = tmp_path / "out"
    panel_dir.mkdir()
    out_dir.mkdir()
    play = tmp_path / "play"
    play.mkdir()
    g = Guardian(play)

    mgr = BackgroundTaskManager(
        panel_dir=panel_dir, tool_results_dir=out_dir, guardian=g,
    )
    # Pass a workdir that points OUTSIDE the guardian — the runner should
    # ignore it and pin to play/.
    bogus_workdir = tmp_path / "elsewhere"
    bogus_workdir.mkdir()
    tid = await mgr.spawn(
        "bash",
        {
            "command": 'pwd && echo "$BUTTERFLY_GUARDIAN_ROOT"',
            "workdir": str(bogus_workdir),
        },
    )

    # Drain until completed.
    deadline = asyncio.get_event_loop().time() + 10
    completed = False
    while asyncio.get_event_loop().time() < deadline:
        try:
            evt = await asyncio.wait_for(mgr.events.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        if evt.tid == tid and evt.kind == "completed":
            completed = True
            break
    assert completed, "bash bg did not complete"

    output = (out_dir / f"{tid}.txt").read_text()
    # Both pwd and the env var must show the guardian root, never the bogus dir.
    assert str(play.resolve()) in output
    assert str(bogus_workdir) not in output


# ── Bug #4: session_shell respects Guardian ───────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="needs bash")
@pytest.mark.asyncio
async def test_session_shell_with_guardian_pins_workdir(tmp_path: Path) -> None:
    from butterfly.tool_engine.executor.terminal.session_shell import SessionShellExecutor

    play = tmp_path / "play"
    play.mkdir()
    g = Guardian(play)
    bogus = tmp_path / "elsewhere"
    bogus.mkdir()

    # Even if caller passes a workdir outside the boundary, Guardian wins.
    sh = SessionShellExecutor(workdir=str(bogus), guardian=g)
    try:
        out = await sh.execute(command='pwd && echo "$BUTTERFLY_GUARDIAN_ROOT"', timeout=10)
        assert str(play.resolve()) in out
        assert str(bogus) not in out
    finally:
        await sh._hard_kill()


# ── Gap #6: sub-agent child sees parent's playground via symlink ──────────────


def _agent_base(tmp_path: Path) -> Path:
    base = tmp_path / "agenthub"
    ag = base / "agent"
    ag.mkdir(parents=True)
    (ag / "config.yaml").write_text("name: agent\nmodel: m\nprovider: anthropic\n", encoding="utf-8")
    (ag / "system.md").write_text("", encoding="utf-8")
    (ag / "task.md").write_text("", encoding="utf-8")
    (ag / "env.md").write_text("", encoding="utf-8")
    (ag / "tools.md").write_text("", encoding="utf-8")
    return base


def test_child_session_links_parent_playground(tmp_path: Path) -> None:
    sessions_base = tmp_path / "sessions"
    sys_base = tmp_path / "_sessions"
    sessions_base.mkdir()
    sys_base.mkdir()
    agent_base = _agent_base(tmp_path)

    # 1) Create the parent session FIRST so its playground exists on disk
    #    when the child tries to link it.
    init_session(
        "parent-1", "agent",
        sessions_base=sessions_base, system_sessions_base=sys_base, agent_base=agent_base,
    )
    parent_play = sessions_base / "parent-1" / "playground"
    (parent_play / "notes.txt").write_text("from parent", encoding="utf-8")

    # 2) Spawn the child with parent_session_id.
    init_session(
        "child-1", "agent",
        sessions_base=sessions_base, system_sessions_base=sys_base, agent_base=agent_base,
        parent_session_id="parent-1",
        mode="explorer",
    )
    link = sessions_base / "child-1" / "playground" / "parent"
    assert link.is_symlink() or link.is_dir(), "expected playground/parent link to exist"
    assert (link / "notes.txt").read_text() == "from parent"

    # 3) Guardian still blocks writes through the symlink (resolves outside).
    g = Guardian(sessions_base / "child-1" / "playground")
    assert g.is_allowed(link / "ok-inside") is False, (
        "writing through playground/parent/ resolves to the parent's tree, "
        "which is outside the child's Guardian root and must be blocked."
    )


def test_child_without_parent_has_no_link(tmp_path: Path) -> None:
    sessions_base = tmp_path / "sessions"
    sys_base = tmp_path / "_sessions"
    sessions_base.mkdir()
    sys_base.mkdir()
    agent_base = _agent_base(tmp_path)
    init_session(
        "lone-1", "agent",
        sessions_base=sessions_base, system_sessions_base=sys_base, agent_base=agent_base,
    )
    assert not (sessions_base / "lone-1" / "playground" / "parent").exists()


# ── Gap #7: HUD endpoint reports running sub_agent count ─────────────────────


def test_hud_endpoint_reports_sub_agent_count(tmp_path: Path) -> None:
    """``get_hud`` returns ``sub_agents_running`` derived from on-disk
    panel entries — frontend uses this to restore the badge after a
    page refresh."""
    from butterfly.service.hud_service import get_hud
    from butterfly.session_engine.panel import (
        TYPE_SUB_AGENT,
        create_pending_tool_entry,
    )

    sessions_base = tmp_path / "sessions"
    sys_base = tmp_path / "_sessions"
    sessions_base.mkdir()
    sys_base.mkdir()
    agent_base = _agent_base(tmp_path)
    init_session(
        "host", "agent",
        sessions_base=sessions_base, system_sessions_base=sys_base, agent_base=agent_base,
    )
    panel_dir = sessions_base / "host" / "core" / "panel"
    # Two running sub_agent entries + one already-completed entry that
    # MUST NOT be counted.
    create_pending_tool_entry(
        panel_dir, tool_name="sub_agent", input={"task": "x", "mode": "explorer"},
        entry_type=TYPE_SUB_AGENT,
    )
    create_pending_tool_entry(
        panel_dir, tool_name="sub_agent", input={"task": "y", "mode": "executor"},
        entry_type=TYPE_SUB_AGENT,
    )
    e3 = create_pending_tool_entry(
        panel_dir, tool_name="sub_agent", input={"task": "z", "mode": "explorer"},
        entry_type=TYPE_SUB_AGENT,
    )
    # Mark one as terminal.
    from butterfly.session_engine.panel import (
        STATUS_COMPLETED, save_entry,
    )
    e3.status = STATUS_COMPLETED
    save_entry(panel_dir, e3)

    hud = get_hud("host", sessions_base, sys_base)
    assert hud["sub_agents_running"] == 2
