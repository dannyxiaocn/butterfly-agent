"""Entity update request management — list, apply, reject pending updates.

Version control
---------------
Each entity has a ``version`` field in its ``agent.yaml``.  When an update is
applied via :func:`apply_update`, the patch number is bumped automatically and
a changelog entry is appended to ``entity/<name>/CHANGELOG.md``.

Agents can read ``entity/<name>/CHANGELOG.md`` (or the version in their
``core/system.md`` that was seeded from the entity) to understand the
evolution of their own configuration.
"""
from __future__ import annotations

import json
import re
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

    # Bump entity version and record changelog entry
    entity_name = _extract_entity_name(record.file_path)
    if entity_name:
        bump_entity_version(entity_name, record, repo_root=repo_root)

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


# ── Entity version control ────────────────────────────────────────────────────

def _extract_entity_name(file_path: str) -> str | None:
    """Extract entity name from a file_path like 'entity/agent/prompts/system.md'."""
    parts = Path(file_path).parts
    if len(parts) >= 2 and parts[0] == "entity":
        return parts[1]
    return None


def _bump_patch(version: str) -> str:
    """Increment the patch component of a semver string (x.y.z → x.y.z+1)."""
    parts = version.lstrip("v").split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except (ValueError, IndexError):
        return "1.0.1"
    return ".".join(parts)


def get_entity_version(entity_name: str, repo_root: Path | None = None) -> str:
    """Return the current version string for an entity (defaults to '1.0.0')."""
    root = repo_root or _REPO_ROOT
    agent_yaml = root / "entity" / entity_name / "agent.yaml"
    if not agent_yaml.exists():
        return "1.0.0"
    text = agent_yaml.read_text(encoding="utf-8")
    m = re.search(r"^version:\s+(\S+)", text, re.MULTILINE)
    return m.group(1) if m else "1.0.0"


def bump_entity_version(
    entity_name: str,
    record: "UpdateRecord",
    repo_root: Path | None = None,
) -> str:
    """Bump entity patch version and append a CHANGELOG entry. Returns new version.

    Modifies ``entity/<name>/agent.yaml`` (version field) and
    ``entity/<name>/CHANGELOG.md`` in place.
    """
    root = repo_root or _REPO_ROOT
    entity_dir = root / "entity" / entity_name
    if not entity_dir.exists():
        return "1.0.0"

    # 1. Bump version in agent.yaml
    agent_yaml = entity_dir / "agent.yaml"
    current = "1.0.0"
    if agent_yaml.exists():
        text = agent_yaml.read_text(encoding="utf-8")
        m = re.search(r"^version:\s+(\S+)", text, re.MULTILINE)
        if m:
            current = m.group(1)
            new_ver = _bump_patch(current)
            text = re.sub(r"^version:\s+\S+", f"version: {new_ver}", text, flags=re.MULTILINE)
        else:
            new_ver = _bump_patch(current)
            # Insert version after the first non-empty line (name:)
            lines = text.splitlines(keepends=True)
            insert_at = 1
            for i, line in enumerate(lines):
                if line.strip() and not line.startswith("#"):
                    insert_at = i + 1
                    break
            lines.insert(insert_at, f"version: {new_ver}\n")
            text = "".join(lines)
        agent_yaml.write_text(text, encoding="utf-8")
    else:
        new_ver = "1.0.1"

    # 2. Append to CHANGELOG.md
    changelog = entity_dir / "CHANGELOG.md"
    entry = (
        f"## v{new_ver} — {record.ts[:19]}\n\n"
        f"**File:** `{record.file_path}`  \n"
        f"**Session:** `{record.session_id}`  \n"
        f"**Reason:** {record.reason}\n\n"
        f"---\n\n"
    )
    if changelog.exists():
        existing = changelog.read_text(encoding="utf-8")
        if existing.startswith("# "):
            # Insert after title line + blank line
            nl = existing.find("\n")
            changelog.write_text(existing[: nl + 1] + "\n" + entry + existing[nl + 1 :], encoding="utf-8")
        else:
            changelog.write_text(entry + existing, encoding="utf-8")
    else:
        changelog.write_text(f"# {entity_name} Changelog\n\n" + entry, encoding="utf-8")

    return new_ver


def get_entity_changelog(entity_name: str, repo_root: Path | None = None) -> str:
    """Return the raw CHANGELOG.md for an entity, or empty string if absent."""
    root = repo_root or _REPO_ROOT
    changelog = root / "entity" / entity_name / "CHANGELOG.md"
    return changelog.read_text(encoding="utf-8") if changelog.exists() else ""
