"""Phase 3: user-input path to the TerminalExecutor + queue service."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from butterfly.session_engine.terminal import TerminalLogger
from butterfly.tool_engine.executor.pure_context.terminal import (
    TerminalExecutor,
)
from butterfly.service.terminal_service import (
    enqueue_input,
    poll_queue,
    read_log_tail,
    read_state,
)


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_user_input_writes_and_logs_as_user_cmd(tmp_path: Path) -> None:
    logger = TerminalLogger(tmp_path / "terminal")
    ex = TerminalExecutor(workdir=str(tmp_path), terminal_logger=logger)
    try:
        # Open the terminal + warm the shell with an agent command.
        await ex.create()
        await ex.use(command="true")

        # User writes a command — include trailing newline so bash runs it.
        ok, output = await ex.user_input("echo hello-from-user\n")
        assert ok is True
        assert "hello-from-user" in output
        # Give the pty a moment to process + log.
        await asyncio.sleep(0.3)

        # Log must carry both the agent's earlier command AND the user's.
        entries = [
            line
            for line in (tmp_path / "terminal" / "log.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        assert any('"source": "agent_cmd"' in e for e in entries)
        assert any('"source": "user_cmd"' in e for e in entries)
        assert any('"source": "user_out"' in e and "hello-from-user" in e for e in entries)
    finally:
        await ex.close()


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_user_input_rejected_while_agent_holds_lock(tmp_path: Path) -> None:
    logger = TerminalLogger(tmp_path / "terminal")
    ex = TerminalExecutor(workdir=str(tmp_path), terminal_logger=logger)
    try:
        await ex.create()
        await ex.use(command="true")  # warm

        # Hold the agent lock with a 1s command.
        agent_task = asyncio.create_task(ex.use(command="sleep 1", timeout=5))
        await asyncio.sleep(0.2)
        assert ex.locked_by_agent is True
        # User tries to inject — must be rejected.
        ok, out = await ex.user_input("echo ignored\n")
        assert ok is False
        assert out == ""

        await agent_task
        assert ex.locked_by_agent is False
        # Post-release user input works.
        ok, out = await ex.user_input("echo works-now\n")
        assert ok is True
        assert "works-now" in out
    finally:
        await ex.close()


def test_input_queue_round_trip(tmp_path: Path) -> None:
    """terminal_service write/read pair used by the daemon poller."""
    sessions_dir = tmp_path
    session_id = "demo"
    (sessions_dir / session_id / "core" / "terminal").mkdir(parents=True)

    id1 = enqueue_input(sessions_dir, session_id, kind="input", content="ls\n")
    id2 = enqueue_input(sessions_dir, session_id, kind="interrupt")

    queue_path = sessions_dir / session_id / "core" / "terminal" / "input.jsonl"
    entries, new_offset = poll_queue(queue_path, 0)
    assert len(entries) == 2
    assert entries[0]["id"] == id1 and entries[0]["type"] == "input"
    assert entries[1]["id"] == id2 and entries[1]["type"] == "interrupt"
    # Re-polling from the returned offset yields nothing.
    more, tail = poll_queue(queue_path, new_offset)
    assert more == []
    assert tail == new_offset


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_read_state_and_log_tail(tmp_path: Path) -> None:
    term_dir = tmp_path / "sessions" / "demo" / "core" / "terminal"
    logger = TerminalLogger(term_dir)
    ex = TerminalExecutor(workdir=str(tmp_path), terminal_logger=logger)
    try:
        await ex.create()
        await ex.use(command="echo tail-probe")
        state = read_state(tmp_path / "sessions", "demo")
        assert state["active"] is True
        log, size = read_log_tail(tmp_path / "sessions", "demo", limit=50)
        assert size > 0
        assert any(
            e.get("source") == "agent_out" and "tail-probe" in e.get("text", "")
            for e in log
        )
    finally:
        await ex.close()


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_use_before_create_returns_canonical_error(tmp_path: Path) -> None:
    """Pin: calling terminal_use before terminal_create must return the
    exact contract string — the v2.0.33 fused shape silently ran
    commands against a freshly re-created shell, which turned "forgot to
    open" into "nothing happened"."""
    ex = TerminalExecutor(workdir=str(tmp_path))
    try:
        out = await ex.use(command="pwd")
        assert out == (
            "Error: Terminal not created, please use terminal_create "
            "tool to create one first"
        )
        # And the shell was NOT spawned as a side-effect.
        assert ex.shell.is_alive() is False
        assert ex.is_created is False
    finally:
        await ex.close()


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_create_returns_welcome_block_with_env_fields(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    ex = TerminalExecutor(workdir=str(tmp_path / "demo"))
    try:
        welcome = await ex.create()
        assert welcome.startswith("[terminal ready]\n")
        lines = welcome.splitlines()
        assert len(lines) == 4
        assert lines[1].startswith("env: ")
        assert lines[2].startswith("path: ")
        assert lines[3].startswith("git: ")
        # cwd should have landed where we spawned.
        assert str(tmp_path / "demo") in lines[2]
        assert ex.is_created is True
    finally:
        await ex.close()


def test_homify_replaces_home_prefix(monkeypatch, tmp_path: Path) -> None:
    """``_homify`` mirrors zsh's ``%~`` expansion so the HUD row reads
    ``(base) ~/work: main`` instead of the full ``/Users/...`` path.
    Paths outside ``$HOME`` pass through untouched."""
    from butterfly.tool_engine.executor.pure_context.terminal import _homify

    fake_home = str(tmp_path / "me")
    (tmp_path / "me").mkdir()
    monkeypatch.setenv("HOME", fake_home)

    assert _homify(fake_home) == "~"
    assert _homify(fake_home + "/work") == "~/work"
    assert _homify(fake_home + "/work/butterfly") == "~/work/butterfly"
    # Non-home prefix: untouched (e.g. /tmp, /var, /opt).
    assert _homify("/tmp/scratch") == "/tmp/scratch"
    # Empty / None passthrough.
    assert _homify(None) is None
    assert _homify("") == ""
    # Tricky: "/Users/melvin" must NOT match "/Users/me" — the ``+"/"``
    # guard in ``_homify`` prevents the prefix-bleed.
    sibling = fake_home + "lvin/work"
    assert _homify(sibling) == sibling


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_detect_env_reports_git_dirty_flag(tmp_path: Path) -> None:
    """Inside a git repo with uncommitted changes, ``_detect_env``
    reports ``git_dirty=True``; after ``git add`` + ``git commit`` it
    flips back to ``False``. Outside a repo we get ``git_dirty=None``
    so the HUD doesn't hang a bogus ``*`` off a null branch."""
    import subprocess as _sp

    repo = tmp_path / "repo"
    repo.mkdir()
    # A known-clean repo: init, configure identity, empty initial commit.
    _sp.check_call(["git", "init", "-q"], cwd=repo)
    _sp.check_call(["git", "config", "user.email", "t@e.x"], cwd=repo)
    _sp.check_call(["git", "config", "user.name", "t"], cwd=repo)
    _sp.check_call(
        ["git", "commit", "--allow-empty", "-q", "-m", "init"], cwd=repo
    )

    from butterfly.tool_engine.executor.pure_context.terminal import (
        TerminalExecutor,
    )
    ex = TerminalExecutor(workdir=str(repo))
    try:
        env = await ex.create()
        fp = ex.last_env
        assert fp is not None
        assert fp.git_branch, "probe should see the default branch"
        assert fp.git_dirty is False, f"clean repo but got {fp.git_dirty!r}"
        # Add an untracked file — porcelain now reports it.
        (repo / "x").write_text("hi")
        out = await ex.use(command="true")
        fp2 = ex.last_env
        assert fp2.git_dirty is True, f"dirty repo but got {fp2.git_dirty!r}"
        # cd out of the repo — flag must become None.
        await ex.use(command=f"cd {tmp_path}")
        fp3 = ex.last_env
        assert fp3.git_branch is None
        assert fp3.git_dirty is None
        # Make sure the welcome string mentions the repo's branch we
        # detected up top — anchors the probe-parsing contract.
        assert fp.git_branch in env
    finally:
        await ex.close()


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_use_prepends_env_banner_after_cd(tmp_path: Path) -> None:
    """When the env fingerprint shifts (cwd here — cheapest to trigger)
    the next ``terminal_use`` output must start with the
    ``[env: … | path: … | git: …]`` banner line."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    ex = TerminalExecutor(workdir=str(tmp_path / "a"))
    try:
        await ex.create()
        # Same cwd — no banner.
        out_same = await ex.use(command="true")
        assert not out_same.lstrip().startswith("[env:")
        # cd into sibling — banner must appear.
        out_changed = await ex.use(command=f"cd {tmp_path / 'b'}")
        first_line = out_changed.splitlines()[0]
        assert first_line.startswith("[env:")
        assert "path: " in first_line
        assert str(tmp_path / "b") in first_line
    finally:
        await ex.close()


@pytest.mark.skipif(sys.platform == "win32", reason="bash required")
@pytest.mark.asyncio
async def test_use_skips_env_probe_when_parked_in_subprocess(tmp_path: Path) -> None:
    """PR review #5: When the pty is parked in a subprocess (python REPL,
    ssh, read -p) ``idle_return=True`` — the env probe would round-trip
    against the wrong process and return ``null_fp``, nulling the HUD
    and prepending ``[env: null | path: null | git: null]``. The fix
    skips the probe in that case."""
    ex = TerminalExecutor(workdir=str(tmp_path))
    try:
        await ex.create()
        # Seed a fingerprint so "banner appeared" is visible.
        fp0 = ex.last_env
        assert fp0 is not None
        # Enter the python3 REPL. ``python3`` with no args prints a
        # banner + prompt and then idles — sentinel won't fire, so
        # ``idle_return=True``.
        out = await ex.use(command="python3 -q", idle_threshold=1.0, timeout=5.0)
        # Fingerprint must NOT have been blanked — the gate prevents the
        # bad probe from landing.
        fp1 = ex.last_env
        assert fp1 is not None
        assert fp1.cwd is not None, "probe ran inside REPL and nulled cwd"
        # Output must NOT carry a spurious [env: null | ...] banner.
        assert "env: null" not in out, f"leaked null banner: {out!r}"
        # Exit the REPL cleanly so the fixture can close.
        await ex.use(command="exit()", idle_threshold=0.8, timeout=5.0)
    finally:
        await ex.close()


def test_format_result_uses_idle_threshold_in_footer(tmp_path: Path) -> None:
    """PR review #7: The "output idle for N s" footer used the
    module-level ``_IDLE_THRESHOLD`` instead of the per-run value,
    contradicting the actual wait when a caller bumped idle_threshold."""
    from butterfly.tool_engine.executor.pure_context.terminal import (
        RunResult,
        _format_result,
    )

    r = RunResult(
        output="",
        exit_code=None,
        duration=5.0,
        timeout=30.0,
        idle_threshold=5.0,
        idle_return=True,
        timed_out=False,
        foreground_cmd="python3",
        foreground_pid=1234,
        shell_alive=True,
        restarted=False,
    )
    footer = _format_result(r)
    assert "output idle for 5.0s" in footer, footer
    assert "output idle for 1.5s" not in footer, footer


def test_read_log_from_handles_multibyte_utf8(tmp_path: Path) -> None:
    """PR review #1: Text-mode byte-offset seek can split multi-byte
    UTF-8 chars and swallow entries silently via ``errors='replace'``.
    Binary-mode read + line-boundary trim must round-trip Chinese text
    cleanly."""
    from butterfly.service.terminal_service import read_log_from
    sid = "sess-42"
    term = tmp_path / sid / "core" / "terminal"
    term.mkdir(parents=True)
    lines = [
        '{"ts": 1.0, "source": "agent_out", "text": "你好"}',
        '{"ts": 2.0, "source": "user_cmd", "text": "ls 中文目录"}',
        '{"ts": 3.0, "source": "agent_out", "text": "café"}',
    ]
    (term / "log.jsonl").write_bytes(
        ("\n".join(lines) + "\n").encode("utf-8")
    )
    # Read from byte 0 — must return all three entries with content
    # intact (decoded via UTF-8, not clobbered by errors='replace').
    entries, new_offset = read_log_from(tmp_path, sid, 0)
    assert len(entries) == 3
    assert entries[0]["text"] == "你好"
    assert entries[1]["text"] == "ls 中文目录"
    assert entries[2]["text"] == "café"
    # Offset must land on a newline boundary so the next call resumes
    # cleanly with no duplicates and no skips.
    assert new_offset == (term / "log.jsonl").stat().st_size

    # Seek mid-file (past the first line, at a newline boundary) —
    # must return entries 2 + 3 without corrupting the "ls 中文目录"
    # payload.
    first_line_len = len(lines[0].encode("utf-8")) + 1  # +1 for \n
    entries2, _ = read_log_from(tmp_path, sid, first_line_len)
    assert len(entries2) == 2
    assert entries2[0]["text"] == "ls 中文目录"
