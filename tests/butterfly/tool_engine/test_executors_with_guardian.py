"""Write / Edit / Bash executors honour an injected Guardian.

Locks down the contract sub_agent's explorer mode depends on:
  - WriteExecutor blocks writes outside the guardian root with a clear error.
  - EditExecutor blocks edits outside the guardian root.
  - BashExecutor pins cwd to the guardian root and exports BUTTERFLY_GUARDIAN_ROOT.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from butterfly.core.guardian import Guardian
from butterfly.tool_engine.executor.terminal.bash_terminal import BashExecutor


@pytest.mark.asyncio
async def test_write_outside_guardian_returns_error(tmp_path: Path) -> None:
    from toolhub.write.executor import WriteExecutor  # noqa: WPS433 (toolhub is a dynamic package)

    root = tmp_path / "play"
    root.mkdir()
    g = Guardian(root)
    w = WriteExecutor(workdir=str(root), guardian=g)

    # Outside path → rejected with PathSandbox-style error.
    out = await w.execute(path=str(tmp_path / "evil.txt"), content="nope")
    assert out.startswith("Error:")
    assert "guardian" in out.lower()
    assert not (tmp_path / "evil.txt").exists()


@pytest.mark.asyncio
async def test_write_inside_guardian_succeeds(tmp_path: Path) -> None:
    from toolhub.write.executor import WriteExecutor

    root = tmp_path / "play"
    root.mkdir()
    g = Guardian(root)
    w = WriteExecutor(workdir=str(root), guardian=g)

    out = await w.execute(path="hello.txt", content="hi")
    assert out.startswith("Wrote ")
    assert (root / "hello.txt").read_text() == "hi"


@pytest.mark.asyncio
async def test_edit_outside_guardian_returns_error(tmp_path: Path) -> None:
    from toolhub.edit.executor import EditExecutor

    root = tmp_path / "play"
    root.mkdir()
    g = Guardian(root)
    target = tmp_path / "outside.txt"
    target.write_text("old content")

    e = EditExecutor(workdir=str(root), guardian=g)
    out = await e.execute(path=str(target), old_string="old", new_string="new")
    assert out.startswith("Error:")
    assert "guardian" in out.lower()
    assert target.read_text() == "old content"


@pytest.mark.skipif(sys.platform == "win32", reason="needs bash")
@pytest.mark.asyncio
async def test_bash_with_guardian_pins_cwd_and_exports_env(tmp_path: Path) -> None:
    root = tmp_path / "play"
    root.mkdir()
    g = Guardian(root)
    b = BashExecutor(timeout=5.0, workdir=str(tmp_path / "ignored"), guardian=g)
    out = await b.execute(command='pwd && echo "$BUTTERFLY_GUARDIAN_ROOT"')
    # Cwd is the guardian root, and the env var matches.
    assert str(root.resolve()) in out
    # The env var line follows pwd; the resolved path appears twice.
    assert out.count(str(root.resolve())) >= 2
