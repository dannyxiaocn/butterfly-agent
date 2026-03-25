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
    bump_entity_version,
    get_entity_version,
    get_entity_changelog,
    _extract_entity_name,
    _bump_patch,
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


# ── version control helpers ───────────────────────────────────────────────────

def test_bump_patch_increments_last_component():
    assert _bump_patch("1.0.0") == "1.0.1"
    assert _bump_patch("1.2.9") == "1.2.10"
    assert _bump_patch("0.0.0") == "0.0.1"


def test_extract_entity_name_from_file_path():
    assert _extract_entity_name("entity/agent/prompts/system.md") == "agent"
    assert _extract_entity_name("entity/nutshell_dev/agent.yaml") == "nutshell_dev"
    assert _extract_entity_name("other/path/file.md") is None


def _make_entity_dir(tmp_path: Path, name: str, version: str = "1.0.0") -> Path:
    """Create a minimal entity directory with agent.yaml."""
    entity_dir = tmp_path / "entity" / name
    entity_dir.mkdir(parents=True)
    (entity_dir / "agent.yaml").write_text(
        f"name: {name}\nversion: {version}\n", encoding="utf-8"
    )
    return entity_dir


def _make_record(file_path: str = "entity/agent/prompts/system.md") -> UpdateRecord:
    return UpdateRecord(
        id="test-id",
        ts="2026-01-15T12:00:00",
        session_id="sess-abc",
        file_path=file_path,
        content="new content",
        reason="Better clarity",
        status="pending",
    )


def test_get_entity_version_reads_yaml(tmp_path):
    _make_entity_dir(tmp_path, "agent", "2.3.4")
    assert get_entity_version("agent", repo_root=tmp_path) == "2.3.4"


def test_get_entity_version_defaults_when_missing(tmp_path):
    assert get_entity_version("nonexistent", repo_root=tmp_path) == "1.0.0"


def test_bump_entity_version_increments_yaml(tmp_path):
    _make_entity_dir(tmp_path, "agent", "1.0.0")
    record = _make_record()
    new_ver = bump_entity_version("agent", record, repo_root=tmp_path)
    assert new_ver == "1.0.1"
    assert get_entity_version("agent", repo_root=tmp_path) == "1.0.1"


def test_bump_entity_version_creates_changelog(tmp_path):
    _make_entity_dir(tmp_path, "agent", "1.0.0")
    record = _make_record()
    bump_entity_version("agent", record, repo_root=tmp_path)
    changelog = get_entity_changelog("agent", repo_root=tmp_path)
    assert "v1.0.1" in changelog
    assert "entity/agent/prompts/system.md" in changelog
    assert "sess-abc" in changelog
    assert "Better clarity" in changelog


def test_bump_entity_version_consecutive_bumps(tmp_path):
    _make_entity_dir(tmp_path, "agent", "1.0.0")
    bump_entity_version("agent", _make_record(), repo_root=tmp_path)
    bump_entity_version("agent", _make_record(), repo_root=tmp_path)
    assert get_entity_version("agent", repo_root=tmp_path) == "1.0.2"
    changelog = get_entity_changelog("agent", repo_root=tmp_path)
    assert "v1.0.1" in changelog
    assert "v1.0.2" in changelog


def test_bump_entity_version_changelog_has_title(tmp_path):
    _make_entity_dir(tmp_path, "myagent", "1.0.0")
    bump_entity_version("myagent", _make_record("entity/myagent/prompts/system.md"), repo_root=tmp_path)
    changelog = get_entity_changelog("myagent", repo_root=tmp_path)
    assert changelog.startswith("# myagent Changelog")


def test_apply_update_bumps_entity_version(tmp_path):
    updates_base = tmp_path / "updates"
    _make_entity_dir(tmp_path, "agent", "1.0.0")
    (tmp_path / "entity" / "agent" / "prompts").mkdir(parents=True)
    (tmp_path / "entity" / "agent" / "prompts" / "system.md").write_text("old content")

    uid = _write_update(updates_base, "entity/agent/prompts/system.md")
    apply_update(uid, updates_base=updates_base, entity_base=tmp_path)

    assert get_entity_version("agent", repo_root=tmp_path) == "1.0.1"
    changelog = get_entity_changelog("agent", repo_root=tmp_path)
    assert "v1.0.1" in changelog


def test_get_entity_changelog_empty_when_no_changelog(tmp_path):
    _make_entity_dir(tmp_path, "agent", "1.0.0")
    assert get_entity_changelog("agent", repo_root=tmp_path) == ""
