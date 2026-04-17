"""Pinning tests from PR #32 review (v2.0.16).

These tests pin the behaviors the review asked the author to change.
When they fail, they point at a concrete line to fix; when they pass,
they prevent regression of the fix.

See PR #32 review comment for the narrative:
https://github.com/dannyxiaocn/butterfly-agent/pull/32
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from conftest import REPO_ROOT


# ── Bug 1: `butterfly chat --keep-alive` shells out to a console script
#         that pyproject.toml no longer exports. ────────────────────────

def test_keepalive_does_not_shell_out_to_removed_butterfly_server_script():
    """`butterfly chat --keep-alive` must not rely on a script that was removed.

    PR #32 deleted the `butterfly-server` console script from pyproject.toml
    but left `ui/cli/chat.py` spawning `subprocess.Popen(["butterfly-server"])`.
    After `pip install -e .` (or an auto-update respawn) the binary vanishes
    and the keep-alive branch dies with FileNotFoundError.

    Fix direction: spawn the daemon via `python -m butterfly.runtime.server`
    or via `butterfly start` (under the unified CLI).
    """
    chat_path = REPO_ROOT / "ui" / "cli" / "chat.py"
    text = chat_path.read_text(encoding="utf-8")
    # No literal reference to the removed console script.
    assert "butterfly-server" not in text, (
        "ui/cli/chat.py still references the removed `butterfly-server` "
        "console script — keep-alive will break after pip install -e . "
        "(the auto-update worker strips this binary)."
    )


def test_pyproject_and_chat_keepalive_agree_on_daemon_entry():
    """Guard: the daemon entry chat.py uses must exist as a console script."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    chat_text = (REPO_ROOT / "ui" / "cli" / "chat.py").read_text(encoding="utf-8")
    for name in ("butterfly-server", "butterfly-web"):
        if name in chat_text:
            assert name in scripts, (
                f"ui/cli/chat.py references `{name}` but pyproject.toml no "
                f"longer exports it as a console script."
            )


# ── Bug 2: `butterfly agent new --blank` scaffolds the legacy filename.

def test_blank_agent_scaffold_writes_tools_md_not_tool_md(tmp_path):
    """`butterfly agent new --blank` should emit `tools.md`, not legacy `tool.md`.

    All agenthub/ agents ship `tools.md` (v2.0.5 convention). The scaffold
    template in ui/cli/new_agent.py::create_agent still writes the legacy
    `tool.md`. Session init has a fallback so the new agent still runs, but
    users who read their scaffolded files get the wrong convention.
    """
    from ui.cli.new_agent import create_agent

    created = create_agent("pr32-probe", tmp_path, init_from=None)
    assert (created / "tools.md").exists(), (
        "Blank scaffold emitted legacy `tool.md` instead of `tools.md`; "
        "update ui/cli/new_agent.py::create_agent."
    )
    assert not (created / "tool.md").exists(), (
        "Scaffold emitted both `tool.md` and `tools.md` — pick one."
    )


# ── Bug 3: docs still advertise removed entry points. ────────────────

_BANNED_IN_DOCS = ("butterfly-server", "butterfly-web")

_DOC_PATHS = [
    REPO_ROOT / "docs" / "ui" / "cli" / "impl.md",
    REPO_ROOT / "docs" / "butterfly" / "impl.md",
    REPO_ROOT / "docs" / "butterfly" / "runtime" / "impl.md",
    REPO_ROOT / "skillhub" / "butterfly" / "SKILL.md",
]


@pytest.mark.parametrize("doc_path", _DOC_PATHS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_user_facing_docs_do_not_advertise_removed_scripts(doc_path: Path):
    """User-facing docs must not tell users to run `butterfly-server`/`butterfly-web`.

    These console scripts were removed from pyproject.toml in v2.0.16 but
    the docs still show them as primary commands. Future readers (human
    and agent) will copy-paste broken commands.
    """
    if not doc_path.exists():
        pytest.skip(f"{doc_path} not present in this checkout")
    text = doc_path.read_text(encoding="utf-8")
    offenders = [s for s in _BANNED_IN_DOCS if s in text]
    assert not offenders, (
        f"{doc_path.relative_to(REPO_ROOT)} still references removed "
        f"console scripts: {offenders}. Rewrite to use the unified `butterfly` CLI."
    )


# ── Positive coverage: rename propagated end-to-end. ─────────────────

def test_create_session_writes_agent_field_in_manifest(tmp_path):
    """`create_session` must persist the renamed `agent` field in manifest.json."""
    import json
    from butterfly.service.sessions_service import create_session

    (tmp_path / "sessions").mkdir()
    (tmp_path / "_sessions").mkdir()
    # Real agent dir lives at REPO_ROOT/agenthub/agent; create_session resolves
    # `agent_base = sessions_dir.parent / "agenthub"`, so point sessions_dir at
    # the repo's own layout.
    result = create_session(
        session_id="pr32-manifest-check",
        agent="agent",
        sessions_dir=REPO_ROOT / "sessions",
        system_sessions_dir=REPO_ROOT / "_sessions",
    )
    assert result["agent"] == "agent"
    manifest_path = REPO_ROOT / "_sessions" / "pr32-manifest-check" / "manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert "agent" in payload, f"manifest missing renamed `agent` field: {payload}"
        assert "entity" not in payload, (
            f"manifest retains obsolete `entity` field (should be dropped): {payload}"
        )
    finally:
        # Cleanup created dirs to avoid polluting subsequent runs.
        import shutil
        shutil.rmtree(REPO_ROOT / "_sessions" / "pr32-manifest-check", ignore_errors=True)
        shutil.rmtree(REPO_ROOT / "sessions" / "pr32-manifest-check", ignore_errors=True)


def test_memory_tools_use_renamed_hub_dirs():
    """Toolhub must ship `memory_recall` / `memory_update` dirs, not the old names."""
    toolhub = REPO_ROOT / "toolhub"
    assert (toolhub / "memory_recall" / "tool.json").exists()
    assert (toolhub / "memory_update" / "tool.json").exists()
    assert not (toolhub / "recall_memory").exists(), (
        "Legacy toolhub/recall_memory/ still present — rename should be hard."
    )
    assert not (toolhub / "update_memory").exists(), (
        "Legacy toolhub/update_memory/ still present — rename should be hard."
    )


# ── Follow-up (round 2): reload-loop risk from the B2 fix. ───────────

def test_server_startup_clears_stale_reload_flag(tmp_path):
    """After a respawn, `update_status.json::reload == True` must not linger.

    The auto-update worker writes `{applied: true, ..., reload: true}` and
    then `os.execvp`s the server. After the new process image enters
    `_run()`, the flag persists until the NEXT worker iteration (default
    3600 s) hits the "no new commits → unlink" branch.

    Meanwhile, `startUpdateNotifier()` reloads unconditionally on
    `reload === true` — so any page load (including the reload itself)
    within that window re-enters the JS, re-polls, re-reloads. Infinite
    reload loop at ~1–2 Hz until the worker's next cycle clears the file.

    Fix direction: `_run()` should clear `reload: true` (or the whole
    status file) as its first post-PID-write step so a post-respawn page
    load only triggers one reload, never a train of them.
    """
    import asyncio
    import json

    system_dir = tmp_path / "_sessions"
    sessions_dir = tmp_path / "sessions"
    system_dir.mkdir()
    sessions_dir.mkdir()
    status_path = system_dir / "update_status.json"
    status_path.write_text(json.dumps({
        "applied": True,
        "new_head": "deadbeef",
        "applied_at": "2026-01-01T00:00:00+00:00",
        "reload": True,
    }))

    from butterfly.runtime import server as srv

    async def _drive_run_briefly():
        # Disable the auto-update worker so no outbound git calls fire.
        import os as _os
        prev = _os.environ.get("BUTTERFLY_AUTOUPDATE_INTERVAL_SEC")
        _os.environ["BUTTERFLY_AUTOUPDATE_INTERVAL_SEC"] = "0"
        try:
            task = asyncio.create_task(srv._run(sessions_dir, system_dir))
            # Give startup time to observe the file and (ideally) clear it.
            await asyncio.sleep(0.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            if prev is None:
                _os.environ.pop("BUTTERFLY_AUTOUPDATE_INTERVAL_SEC", None)
            else:
                _os.environ["BUTTERFLY_AUTOUPDATE_INTERVAL_SEC"] = prev

    asyncio.run(_drive_run_briefly())

    if not status_path.exists():
        return  # whole file cleared — also acceptable

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert not payload.get("reload"), (
        "Stale `reload: true` still present after server startup. The "
        "auto-update worker writes this before `execvp`, and the next "
        "clear happens only in the `no new commits` branch after "
        "`interval_sec` elapses — leaving a window during which the "
        "frontend polls, reloads, and loops. Clear the flag (or the "
        "entire status file) in `_run()` after `_write_pid`."
    )


def test_auto_update_worker_and_cmd_update_agree_on_dirty_check():
    """Both dirty-tree detectors must treat untracked files as unsafe to pull.

    `cmd_update` was upgraded to `git status --porcelain` so it refuses
    when upstream would collide with a local untracked file. The
    `_auto_update_worker` still uses `git diff --quiet` + `git diff
    --cached --quiet`, which miss untracked files. In that case the
    worker concludes "clean" and attempts `git pull --ff-only`, which
    Git itself refuses — but instead of surfacing the dirty-tree UI
    banner, the worker just logs `git pull failed` and silently keeps
    looping.

    Fix direction: switch the worker's `dirty` computation to
    `git status --porcelain` for parity with `cmd_update`.
    """
    server_src = (REPO_ROOT / "butterfly" / "runtime" / "server.py").read_text(encoding="utf-8")
    main_src = (REPO_ROOT / "ui" / "cli" / "main.py").read_text(encoding="utf-8")
    uses_porcelain_in_cmd = "status --porcelain" in main_src or "status\", \"--porcelain" in main_src
    uses_porcelain_in_worker = "status --porcelain" in server_src or "status\", \"--porcelain" in server_src
    if uses_porcelain_in_cmd:
        assert uses_porcelain_in_worker, (
            "cmd_update uses `git status --porcelain` for dirty detection "
            "but _auto_update_worker still relies on `git diff` (misses "
            "untracked files). Align the worker with the stricter check."
        )
