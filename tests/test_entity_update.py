"""Tests for entity update request system."""
from __future__ import annotations

import json
import pytest
from pathlib import Path

from nutshell.tool_engine.providers.entity_update import (
    propose_entity_update,
    _UPDATES_DIR_NAME,
)
from nutshell.runtime.entity_updates import (
    list_pending_updates,
    apply_update,
    reject_update,
    UpdateRecord,
)


# ── propose_entity_update tool ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_propose_writes_pending_update(tmp_path, monkeypatch):
    monkeypatch.setenv("NUTSHELL_SESSION_ID", "test-session")
    entity_dir = tmp_path / "entity"
    entity_dir.mkdir()

    result = await propose_entity_update(
        file_path="entity/agent/prompts/system.md",
        content="New system prompt content.",
        reason="Improve clarity",
        _entity_base=entity_dir,
        _updates_base=tmp_path / _UPDATES_DIR_NAME,
    )

    assert "submitted" in result.lower() or "pending" in result.lower()

    updates_dir = tmp_path / _UPDATES_DIR_NAME
    files = list(updates_dir.glob("*.json"))
    assert len(files) == 1

    record = json.loads(files[0].read_text())
    assert record["file_path"] == "entity/agent/prompts/system.md"
    assert record["content"] == "New system prompt content."
    assert record["reason"] == "Improve clarity"
    assert record["session_id"] == "test-session"
    assert record["status"] == "pending"


@pytest.mark.asyncio
async def test_propose_rejects_path_outside_entity(tmp_path, monkeypatch):
    monkeypatch.setenv("NUTSHELL_SESSION_ID", "test-session")
    entity_dir = tmp_path / "entity"
    entity_dir.mkdir()

    result = await propose_entity_update(
        file_path="../outside/file.md",
        content="malicious",
        reason="test",
        _entity_base=entity_dir,
        _updates_base=tmp_path / _UPDATES_DIR_NAME,
    )

    assert "error" in result.lower() or "invalid" in result.lower()
    # Nothing should be written
    updates_dir = tmp_path / _UPDATES_DIR_NAME
    assert not updates_dir.exists() or len(list(updates_dir.glob("*.json"))) == 0


@pytest.mark.asyncio
async def test_propose_rejects_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setenv("NUTSHELL_SESSION_ID", "test-session")
    entity_dir = tmp_path / "entity"
    entity_dir.mkdir()

    result = await propose_entity_update(
        file_path="/etc/passwd",
        content="malicious",
        reason="test",
        _entity_base=entity_dir,
        _updates_base=tmp_path / _UPDATES_DIR_NAME,
    )

    assert "error" in result.lower() or "invalid" in result.lower()


# ── list_pending_updates ──────────────────────────────────────────────────────

def test_list_pending_updates_empty(tmp_path):
    updates = list_pending_updates(tmp_path / "nonexistent")
    assert updates == []


def _write_update(updates_base: Path, file_path: str, status: str = "pending") -> str:
    import uuid
    updates_base.mkdir(parents=True, exist_ok=True)
    uid = str(uuid.uuid4())
    record = {
        "id": uid,
        "ts": "2026-01-01T00:00:00",
        "session_id": "sess",
        "file_path": file_path,
        "content": "new content",
        "reason": "some reason",
        "status": status,
    }
    (updates_base / f"{uid}.json").write_text(json.dumps(record))
    return uid


def test_list_pending_updates_returns_only_pending(tmp_path):
    updates_base = tmp_path / "updates"
    _write_update(updates_base, "entity/agent/prompts/system.md", "pending")
    _write_update(updates_base, "entity/agent/prompts/system.md", "applied")
    _write_update(updates_base, "entity/agent/prompts/system.md", "rejected")

    pending = list_pending_updates(updates_base)
    assert len(pending) == 1
    assert pending[0].status == "pending"


# ── apply_update ──────────────────────────────────────────────────────────────

def test_apply_update_writes_file(tmp_path):
    # entity_base param is the REPO ROOT; file_path in record is relative to repo root
    updates_base = tmp_path / "updates"
    (tmp_path / "entity" / "agent" / "prompts").mkdir(parents=True)
    (tmp_path / "entity" / "agent" / "prompts" / "system.md").write_text("old content")

    uid = _write_update(updates_base, "entity/agent/prompts/system.md")
    apply_update(uid, updates_base=updates_base, entity_base=tmp_path)

    assert (tmp_path / "entity" / "agent" / "prompts" / "system.md").read_text() == "new content"

    # Record status should be updated to "applied"
    record = json.loads((updates_base / f"{uid}.json").read_text())
    assert record["status"] == "applied"


def test_apply_update_creates_parent_dirs(tmp_path):
    updates_base = tmp_path / "updates"
    # No pre-existing file; apply_update should create parent dirs
    uid = _write_update(updates_base, "entity/agent/skills/new-skill/SKILL.md")
    apply_update(uid, updates_base=updates_base, entity_base=tmp_path)

    target = tmp_path / "entity" / "agent" / "skills" / "new-skill" / "SKILL.md"
    assert target.exists()
    assert target.read_text() == "new content"


def test_apply_update_raises_for_unknown_id(tmp_path):
    updates_base = tmp_path / "updates"
    updates_base.mkdir()
    with pytest.raises(FileNotFoundError):
        apply_update("nonexistent-id", updates_base=updates_base, entity_base=tmp_path)


# ── reject_update ─────────────────────────────────────────────────────────────

def test_reject_update_marks_rejected(tmp_path):
    updates_base = tmp_path / "updates"
    uid = _write_update(updates_base, "entity/agent/prompts/system.md")
    reject_update(uid, updates_base=updates_base)

    record = json.loads((updates_base / f"{uid}.json").read_text())
    assert record["status"] == "rejected"


# ── registry integration ──────────────────────────────────────────────────────

def test_propose_entity_update_registered_as_builtin():
    from nutshell.tool_engine.registry import get_builtin
    impl = get_builtin("propose_entity_update")
    assert impl is not None
    assert callable(impl)
