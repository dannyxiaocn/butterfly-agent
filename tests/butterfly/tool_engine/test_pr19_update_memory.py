"""PR #19 review coverage: memory_update executor + main-memory index sync.

Checks:
- Creation writes sub-memory and upserts index line.
- Edit syncs/upserts index line (only when description is provided).
- Path-traversal attempts are rejected.
- Main memory missing → tool creates index section.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from toolhub.memory_update.executor import MemoryUpdateExecutor


@pytest.mark.asyncio
async def test_create_sub_memory_writes_file_and_index(tmp_path: Path) -> None:
    mem_dir = tmp_path / "memory"
    main_mem = tmp_path / "memory.md"
    main_mem.write_text("# MEMORY\n", encoding="utf-8")

    ex = MemoryUpdateExecutor(memory_dir=mem_dir, main_memory_path=main_mem)
    out = await ex.execute(
        name="repo_map",
        old_string="",
        new_string="- root: src/",
        description="cached repo structure",
    )
    assert "Created" in out
    assert (mem_dir / "repo_map.md").read_text() == "- root: src/"
    assert "- repo_map: cached repo structure" in main_mem.read_text()


@pytest.mark.asyncio
async def test_create_requires_description(tmp_path: Path) -> None:
    ex = MemoryUpdateExecutor(
        memory_dir=tmp_path / "memory", main_memory_path=tmp_path / "memory.md"
    )
    (tmp_path / "memory.md").write_text("# Main\n", encoding="utf-8")
    out = await ex.execute(name="x", old_string="", new_string="body")
    assert out.startswith("Error:") and "description" in out


@pytest.mark.asyncio
async def test_edit_existing_sub_memory(tmp_path: Path) -> None:
    mem_dir = tmp_path / "memory"
    main_mem = tmp_path / "memory.md"
    mem_dir.mkdir()
    (mem_dir / "notes.md").write_text("alpha\nbeta\n", encoding="utf-8")
    main_mem.write_text(
        "# MEMORY\n\n## Memory files\n- notes: short notes\n", encoding="utf-8"
    )

    ex = MemoryUpdateExecutor(memory_dir=mem_dir, main_memory_path=main_mem)
    out = await ex.execute(name="notes", old_string="beta", new_string="bravo")
    assert "Replaced 1" in out
    assert (mem_dir / "notes.md").read_text() == "alpha\nbravo\n"


@pytest.mark.asyncio
async def test_invalid_name_rejected(tmp_path: Path) -> None:
    ex = MemoryUpdateExecutor(
        memory_dir=tmp_path / "memory", main_memory_path=tmp_path / "memory.md"
    )
    for bad in ["../../../etc/passwd", "foo/bar", "has space", "", "dot.ted"]:
        out = await ex.execute(
            name=bad, old_string="", new_string="x", description="evil"
        )
        assert out.startswith("Error:") and "invalid" in out.lower()


@pytest.mark.asyncio
async def test_main_memory_missing_creates_index(tmp_path: Path) -> None:
    """If main memory.md doesn't exist, the tool writes a fresh one with the index."""
    mem_dir = tmp_path / "memory"
    main_mem = tmp_path / "does_not_exist.md"
    assert not main_mem.exists()

    ex = MemoryUpdateExecutor(memory_dir=mem_dir, main_memory_path=main_mem)
    out = await ex.execute(
        name="first",
        old_string="",
        new_string="hello",
        description="first entry",
    )
    assert "Created" in out
    assert main_mem.exists()
    assert "- first: first entry" in main_mem.read_text()


@pytest.mark.asyncio
async def test_edit_without_description_preserves_index(tmp_path: Path) -> None:
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    main_mem = tmp_path / "memory.md"
    (mem_dir / "notes.md").write_text("abc", encoding="utf-8")
    main_mem.write_text(
        "# MEMORY\n\n## Memory files\n- notes: keep-me\n", encoding="utf-8"
    )

    ex = MemoryUpdateExecutor(memory_dir=mem_dir, main_memory_path=main_mem)
    # No description given; edit should not rewrite the index line.
    await ex.execute(name="notes", old_string="abc", new_string="xyz")
    assert "- notes: keep-me" in main_mem.read_text()


@pytest.mark.asyncio
async def test_tool_without_config_errors(tmp_path: Path) -> None:
    ex = MemoryUpdateExecutor(memory_dir=None, main_memory_path=None)
    out = await ex.execute(name="x", old_string="", new_string="y", description="z")
    assert out.startswith("Error:")
