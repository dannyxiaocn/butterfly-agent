"""glob + grep executors."""
from __future__ import annotations

from pathlib import Path

import pytest

from toolhub.glob.executor import GlobExecutor
from toolhub.grep.executor import GrepExecutor


@pytest.mark.asyncio
async def test_glob_filename_pattern_recursive(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("y", encoding="utf-8")
    (tmp_path / "sub" / "c.txt").write_text("z", encoding="utf-8")

    out = await GlobExecutor(workdir=str(tmp_path)).execute(pattern="*.py")
    lines = set(out.splitlines())
    assert "a.py" in lines
    assert "sub/b.py" in lines
    assert "sub/c.txt" not in out


@pytest.mark.asyncio
async def test_glob_no_matches(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    out = await GlobExecutor(workdir=str(tmp_path)).execute(pattern="*.nonesuch")
    assert "No files matched" in out


@pytest.mark.asyncio
async def test_glob_path_aware_pattern(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "one.py").write_text("x", encoding="utf-8")
    (tmp_path / "elsewhere.py").write_text("y", encoding="utf-8")
    out = await GlobExecutor(workdir=str(tmp_path)).execute(pattern="src/*.py")
    assert "src/one.py" in out
    assert "elsewhere.py" not in out


@pytest.mark.asyncio
async def test_grep_basic_content(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("alpha\nbravo\ncharlie\n", encoding="utf-8")
    out = await GrepExecutor(workdir=str(tmp_path)).execute(
        pattern="bravo", output_mode="content"
    )
    assert "bravo" in out
    # Path prefix is produced by either rg or the python fallback.
    assert "f.txt" in out


@pytest.mark.asyncio
async def test_grep_no_match(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("alpha\n", encoding="utf-8")
    out = await GrepExecutor(workdir=str(tmp_path)).execute(pattern="zzznone")
    assert "No matches" in out


@pytest.mark.asyncio
async def test_grep_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("Hello World\n", encoding="utf-8")
    # The schema uses `-i` as the flag name (mirrors ripgrep CLI).
    out = await GrepExecutor(workdir=str(tmp_path)).execute(
        pattern="hello", output_mode="content", **{"-i": True}
    )
    assert "Hello World" in out


@pytest.mark.asyncio
async def test_grep_files_with_matches(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("foo bar\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("only bar\n", encoding="utf-8")
    out = await GrepExecutor(workdir=str(tmp_path)).execute(
        pattern="foo", output_mode="files_with_matches"
    )
    assert "a.txt" in out
    assert "b.txt" not in out
