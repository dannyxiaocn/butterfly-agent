"""Guardian — path boundary used by sub-agent's explorer mode.

Covers the contract Write/Edit/Bash rely on:
  - Paths inside root are allowed (relative + absolute).
  - Paths outside root are rejected with PermissionError.
  - Symlink escape attempts resolve through the link first, so they fail.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from butterfly.core.guardian import Guardian


def test_guardian_allows_paths_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "play"
    root.mkdir()
    g = Guardian(root)
    g.check_write(root / "a.txt")
    g.check_write("nested/b.txt")
    g.check_write(str(root / "deep" / "c.txt"))


def test_guardian_rejects_paths_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "play"
    root.mkdir()
    outside = tmp_path / "elsewhere" / "x.txt"
    outside.parent.mkdir(parents=True)
    g = Guardian(root)
    with pytest.raises(PermissionError):
        g.check_write(outside)
    with pytest.raises(PermissionError):
        # Absolute path that ../-escapes the root
        g.check_write(str(root / ".." / "elsewhere" / "y.txt"))


def test_guardian_blocks_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "play"
    root.mkdir()
    outside_dir = tmp_path / "secret"
    outside_dir.mkdir()
    link = root / "trapdoor"
    os.symlink(outside_dir, link)  # play/trapdoor → secret/
    g = Guardian(root)
    # Writing through the symlink resolves to outside the sandbox; reject.
    with pytest.raises(PermissionError):
        g.check_write(link / "leak.txt")


def test_guardian_is_allowed_helper(tmp_path: Path) -> None:
    root = tmp_path / "play"
    root.mkdir()
    g = Guardian(root)
    assert g.is_allowed(root / "ok.txt") is True
    assert g.is_allowed(tmp_path / "out.txt") is False


def test_guardian_repr_contains_root(tmp_path: Path) -> None:
    g = Guardian(tmp_path)
    assert "Guardian" in repr(g)
    assert str(tmp_path.resolve()) in repr(g)
