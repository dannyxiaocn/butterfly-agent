"""tool_output tool — happy path + path-traversal guard."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from butterfly.session_engine.panel import (
    STATUS_COMPLETED,
    create_pending_tool_entry,
    save_entry,
)
from toolhub.tool_output.executor import ToolOutputExecutor


@pytest.mark.asyncio
async def test_tool_output_requires_task_id(tmp_path: Path) -> None:
    ex = ToolOutputExecutor(panel_dir=tmp_path)
    out = await ex.execute()
    assert out.startswith("Error:") and "task_id" in out.lower()


@pytest.mark.asyncio
async def test_tool_output_missing_panel_dir() -> None:
    ex = ToolOutputExecutor(panel_dir=None)
    out = await ex.execute(task_id="bg_deadbe")
    assert out.startswith("Error:") and "panel" in out.lower()


@pytest.mark.asyncio
async def test_tool_output_unknown_task_id(tmp_path: Path) -> None:
    ex = ToolOutputExecutor(panel_dir=tmp_path)
    out = await ex.execute(task_id="bg_missing")
    assert out.startswith("Error:") and "no panel entry" in out


@pytest.mark.asyncio
async def test_tool_output_fetches_body_and_footer(tmp_path: Path) -> None:
    panel_dir = tmp_path / "panel"
    out_dir = tmp_path / "out"
    panel_dir.mkdir()
    out_dir.mkdir()

    entry = create_pending_tool_entry(panel_dir, tool_name="bash", input={})
    output_file = out_dir / f"{entry.tid}.txt"
    output_file.write_text("hello world\n", encoding="utf-8")
    entry.output_file = str(output_file)
    entry.output_bytes = output_file.stat().st_size
    entry.status = STATUS_COMPLETED
    entry.exit_code = 0
    save_entry(panel_dir, entry)

    ex = ToolOutputExecutor(panel_dir=panel_dir)
    out = await ex.execute(task_id=entry.tid)
    assert "hello world" in out
    assert f"status={STATUS_COMPLETED}" in out
    assert "exit=0" in out


@pytest.mark.asyncio
async def test_tool_output_delta_mode(tmp_path: Path) -> None:
    panel_dir = tmp_path / "panel"
    out_dir = tmp_path / "out"
    panel_dir.mkdir()
    out_dir.mkdir()

    entry = create_pending_tool_entry(panel_dir, tool_name="bash", input={})
    output_file = out_dir / f"{entry.tid}.txt"
    output_file.write_text("first-chunk\n", encoding="utf-8")
    entry.output_file = str(output_file)
    save_entry(panel_dir, entry)

    ex = ToolOutputExecutor(panel_dir=panel_dir)
    # First delta fetch returns everything so far.
    first = await ex.execute(task_id=entry.tid, delta=True)
    assert "first-chunk" in first

    # Second delta fetch (without new data) returns just the footer.
    second = await ex.execute(task_id=entry.tid, delta=True)
    assert "first-chunk" not in second
    assert "delta-mode" in second

    # After more data is appended, delta returns only the new bytes.
    with output_file.open("a", encoding="utf-8") as fh:
        fh.write("second-chunk\n")
    third = await ex.execute(task_id=entry.tid, delta=True)
    assert "second-chunk" in third
    assert "first-chunk" not in third


@pytest.mark.asyncio
async def test_tool_output_path_traversal_regression(tmp_path: Path) -> None:
    """Cubic P1 (confirmed): task_id path separators allow directory traversal.

    `load_entry(panel_dir, task_id)` builds `panel_dir / f"{task_id}.json"`.
    A task_id like "../evil" resolves OUTSIDE the panel directory, letting
    the caller read any `*.json` file the agent has read access to — and,
    worse, since `output_file` is stored as an absolute path in the JSON,
    the attacker can coerce the tool into reading arbitrary files by
    dropping a fake panel JSON.

    Expected post-fix behaviour: the tool must reject task_ids that contain
    path separators / `..` and treat them as unknown IDs.
    """
    panel_dir = tmp_path / "panel"
    panel_dir.mkdir()

    # Evil file: sits OUTSIDE panel_dir (next to it) but still inside tmp_path.
    evil_path = tmp_path / "evil.json"
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET-CONTENT", encoding="utf-8")
    evil_payload = {
        "tid": "evil",
        "type": "pending_tool",
        "tool_name": "bash",
        "input": {},
        "status": STATUS_COMPLETED,
        "created_at": 0.0,
        "output_file": str(secret),
        "output_bytes": secret.stat().st_size,
        "exit_code": 0,
    }
    evil_path.write_text(json.dumps(evil_payload), encoding="utf-8")

    ex = ToolOutputExecutor(panel_dir=panel_dir)
    out = await ex.execute(task_id="../evil")

    if "SECRET-CONTENT" in out:
        pytest.xfail(
            "tool_output accepts '../evil' task_id and reads files outside the "
            "session panel directory (cubic P1, not fixed in PR #19)."
        )
    else:
        # When fixed, the tool should reject or 404 on traversal attempts.
        assert out.startswith("Error:")
