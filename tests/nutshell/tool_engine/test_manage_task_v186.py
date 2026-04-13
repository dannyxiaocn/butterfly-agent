"""Tests for v1.3.86 manage_task changes: resume action, pending default status."""
from __future__ import annotations

import json
import pytest
from pathlib import Path


@pytest.fixture
def executor(tmp_path):
    from toolhub.manage_task.executor import ManageTaskExecutor
    return ManageTaskExecutor(tasks_dir=tmp_path)


@pytest.mark.asyncio
async def test_create_default_status_pending(executor):
    """Newly created tasks should have status=pending."""
    await executor.execute(action="create", name="t1", description="test")
    data = json.loads((executor._tasks_dir / "t1.json").read_text())
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_resume_action(executor):
    """'resume' sets status to pending."""
    await executor.execute(action="create", name="t1")
    await executor.execute(action="pause", name="t1")

    data = json.loads((executor._tasks_dir / "t1.json").read_text())
    assert data["status"] == "paused"

    result = await executor.execute(action="resume", name="t1")
    assert "pending" in result

    data = json.loads((executor._tasks_dir / "t1.json").read_text())
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_resume_missing_task(executor):
    """Resume on nonexistent task returns error."""
    result = await executor.execute(action="resume", name="nope")
    assert "not found" in result


@pytest.mark.asyncio
async def test_error_message_mentions_resume(executor):
    """Error for missing name includes 'resume' in required actions."""
    result = await executor.execute(action="resume")
    assert "resume" in result.lower() or "required" in result.lower()


@pytest.mark.asyncio
async def test_pause_resume_cycle(executor):
    """Full cycle: create → pause → resume → finish."""
    await executor.execute(action="create", name="cycle")

    data = json.loads((executor._tasks_dir / "cycle.json").read_text())
    assert data["status"] == "pending"

    await executor.execute(action="pause", name="cycle")
    data = json.loads((executor._tasks_dir / "cycle.json").read_text())
    assert data["status"] == "paused"

    await executor.execute(action="resume", name="cycle")
    data = json.loads((executor._tasks_dir / "cycle.json").read_text())
    assert data["status"] == "pending"

    await executor.execute(action="finish", name="cycle")
    data = json.loads((executor._tasks_dir / "cycle.json").read_text())
    assert data["status"] == "finished"
