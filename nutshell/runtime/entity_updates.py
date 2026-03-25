"""Entity update request management — list, apply, reject pending updates."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_UPDATES_BASE = _REPO_ROOT / "_entity_updates"


@dataclass
class UpdateRecord:
    id: str
    ts: str
    session_id: str
    file_path: str
    content: str
    reason: str
    status: str

    @classmethod
    def from_dict(cls, d: dict) -> "UpdateRecord":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__})


def _load_record(path: Path) -> UpdateRecord:
    return UpdateRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _save_record(record: UpdateRecord, updates_base: Path) -> None:
    path = updates_base / f"{record.id}.json"
    data = {k: getattr(record, k) for k in record.__dataclass_fields__}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def list_pending_updates(updates_base: Path | None = None) -> list[UpdateRecord]:
    """Return all pending UpdateRecord objects, sorted by timestamp."""
    base = updates_base or _DEFAULT_UPDATES_BASE
    if not base.exists():
        return []
    records = []
    for path in sorted(base.glob("*.json")):
        try:
            record = _load_record(path)
            if record.status == "pending":
                records.append(record)
        except Exception:
            continue
    return sorted(records, key=lambda r: r.ts)


def apply_update(
    update_id: str,
    *,
    updates_base: Path | None = None,
    entity_base: Path | None = None,
) -> None:
    """Apply a pending update: write content to entity file, mark as 'applied'.

    Args:
        entity_base: Repo root (file_path in the record is relative to repo root,
                     e.g. 'entity/agent/prompts/system.md'). Defaults to repo root.
    """
    base = updates_base or _DEFAULT_UPDATES_BASE
    repo_root = entity_base or _REPO_ROOT

    record_path = base / f"{update_id}.json"
    if not record_path.exists():
        raise FileNotFoundError(f"Update record not found: {update_id}")

    record = _load_record(record_path)

    # file_path is relative to repo root (e.g. "entity/agent/prompts/system.md")
    target = repo_root / record.file_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(record.content, encoding="utf-8")

    record.status = "applied"
    _save_record(record, base)


def reject_update(
    update_id: str,
    *,
    updates_base: Path | None = None,
) -> None:
    """Mark a pending update as 'rejected'."""
    base = updates_base or _DEFAULT_UPDATES_BASE
    record_path = base / f"{update_id}.json"
    if not record_path.exists():
        raise FileNotFoundError(f"Update record not found: {update_id}")

    record = _load_record(record_path)
    record.status = "rejected"
    _save_record(record, base)
