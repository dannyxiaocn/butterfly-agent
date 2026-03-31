from __future__ import annotations

import shutil
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SESSIONS_DIR = _REPO_ROOT / 'sessions'


def get_meta_session_id(entity_name: str) -> str:
    return f"{entity_name}_meta"


def get_meta_dir(entity_name: str) -> Path:
    return _SESSIONS_DIR / get_meta_session_id(entity_name)


def ensure_meta_session(entity_name: str) -> Path:
    """Create sessions/<entity>_meta/ directory structure (idempotent)."""
    session_dir = get_meta_dir(entity_name)
    core_dir = session_dir / 'core'
    core_dir.mkdir(parents=True, exist_ok=True)
    (core_dir / 'tools').mkdir(exist_ok=True)
    (core_dir / 'skills').mkdir(exist_ok=True)
    (session_dir / 'docs').mkdir(exist_ok=True)
    (session_dir / 'playground').mkdir(exist_ok=True)
    for fname in ('system.md', 'heartbeat.md', 'session.md', 'memory.md', 'tasks.md'):
        (core_dir / fname).touch(exist_ok=True)
    return session_dir


def sync_from_entity(entity_name: str, entity_base: Path | None = None) -> None:
    """Bootstrap meta-session memory from entity memory on first use."""
    entity_root = entity_base or (_REPO_ROOT / 'entity')
    entity_dir = entity_root / entity_name
    meta_dir = ensure_meta_session(entity_name)
    core_dir = meta_dir / 'core'
    meta_memory = core_dir / 'memory.md'
    if meta_memory.exists() and meta_memory.read_text(encoding='utf-8').strip():
        return

    entity_memory = entity_dir / 'memory.md'
    if entity_memory.exists() and not meta_memory.read_text(encoding='utf-8'):
        meta_memory.write_text(entity_memory.read_text(encoding='utf-8'), encoding='utf-8')

    entity_memory_dir = entity_dir / 'memory'
    if entity_memory_dir.is_dir():
        meta_memory_dir = core_dir / 'memory'
        meta_memory_dir.mkdir(parents=True, exist_ok=True)
        for src_file in sorted(entity_memory_dir.glob('*.md')):
            dst_file = meta_memory_dir / src_file.name
            if not dst_file.exists():
                shutil.copy2(src_file, dst_file)
