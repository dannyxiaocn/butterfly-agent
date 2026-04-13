"""Shared session initialization — creates session directory structure from an entity.

Used by:
  - ui/web/sessions.py  (web UI new-session endpoint)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from nutshell.session_engine.session_config import read_config, write_config, ensure_config
from nutshell.session_engine.session_status import ensure_session_status, write_session_status
from nutshell.session_engine.task_cards import ensure_card
from nutshell.session_engine.entity_state import (
    ensure_gene_initialized,
    ensure_meta_session,
    get_meta_version,
    populate_meta_from_entity,
    start_meta_agent,
    sync_from_entity,
)

_REPO_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_SESSIONS_BASE = _REPO_ROOT / "sessions"
_DEFAULT_SYSTEM_SESSIONS_BASE = _REPO_ROOT / "_sessions"
_DEFAULT_ENTITY_BASE = _REPO_ROOT / "entity"


def _write_if_absent(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _is_real_memory(content: str) -> bool:
    """Return True if content has meaningful memory, not just a seed placeholder."""
    stripped = content.strip()
    if not stripped:
        return False
    # Filter out placeholder-only content: lines that are headings or "(empty..." markers
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    return any(not ln.startswith("#") and not ln.startswith("(empty") for ln in lines)


def _create_session_venv(session_dir: Path) -> Path:
    """Create a Python venv at session_dir/.venv (idempotent).

    Uses --system-site-packages so all globally installed packages are
    available without re-installing.  Returns the venv path.

    Race-safe: if two processes attempt concurrent creation (same session_id
    generated within the same second), the loser catches CalledProcessError
    and returns the venv that the winner already created.
    """
    venv_path = session_dir / ".venv"
    if venv_path.exists():
        return venv_path
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(venv_path)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        # Another process may have won the race and already created a valid venv.
        # Check pyvenv.cfg (created last by venv) as the completion sentinel.
        if (venv_path / "pyvenv.cfg").exists():
            return venv_path
        raise
    return venv_path


def init_session(
    session_id: str,
    entity_name: str,
    *,
    sessions_base: Path | None = None,
    system_sessions_base: Path | None = None,
    entity_base: Path | None = None,
    initial_message: str | None = None,
    **_kwargs,  # absorb legacy heartbeat= for backward compat
) -> str:
    """Create a new session on disk from an entity, ready for the server to pick up.

    Returns the session_id. Idempotent: only writes files that do not exist yet.

    Args:
        session_id:          The unique session identifier (e.g. '2026-03-25_10-00-00').
        entity_name:         Name of the entity in entity_base/ (e.g. 'agent', 'nutshell_dev').
        sessions_base:       Root of agent-visible sessions/ directory.
        system_sessions_base: Root of _sessions/ directory.
        entity_base:         Root of entity/ directory.
        initial_message:     Optional first user message to write to context.jsonl.
    """
    s_base = sessions_base or _DEFAULT_SESSIONS_BASE
    sys_base = system_sessions_base or _DEFAULT_SYSTEM_SESSIONS_BASE
    ent_base = entity_base or _DEFAULT_ENTITY_BASE

    session_dir = s_base / session_id
    system_dir = sys_base / session_id
    core_dir = session_dir / "core"

    # Create directory tree
    core_dir.mkdir(parents=True, exist_ok=True)
    (core_dir / "tools").mkdir(exist_ok=True)
    (core_dir / "skills").mkdir(exist_ok=True)
    (session_dir / "docs").mkdir(exist_ok=True)
    (session_dir / "playground").mkdir(exist_ok=True)
    system_dir.mkdir(parents=True, exist_ok=True)

    context_path = system_dir / "context.jsonl"
    events_path = system_dir / "events.jsonl"
    context_path.touch(exist_ok=True)
    events_path.touch(exist_ok=True)

    # Create session-level Python venv (idempotent)
    _create_session_venv(session_dir)

    # Write manifest (always overwritten so entity is current)
    manifest = {
        "session_id": session_id,
        "entity": entity_name,
        "created_at": datetime.now().isoformat(),
    }
    (system_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    entity_dir = ent_base / entity_name

    # Config always comes from meta session; meta is initially populated from entity.
    meta_dir = ensure_meta_session(entity_name, s_base=s_base)
    if entity_dir.exists():
        meta_config = meta_dir / 'core' / 'config.yaml'
        if not meta_config.exists() or not meta_config.read_text(encoding='utf-8').strip():
            populate_meta_from_entity(entity_name, ent_base, s_base)
        ensure_gene_initialized(entity_name, ent_base, s_base)
        start_meta_agent(entity_name, entity_base=ent_base, s_base=s_base, sys_base=sys_base)

    meta_core_dir = meta_dir / "core"
    # Copy prompts with new names (task.md, env.md) and fallback to old names (heartbeat.md, session.md)
    for new_name, old_name in [("system.md", None), ("task.md", "heartbeat.md"), ("env.md", "session.md")]:
        src = meta_core_dir / new_name
        if not src.exists() and old_name:
            src = meta_core_dir / old_name
        _write_if_absent(core_dir / new_name, src.read_text(encoding="utf-8") if src.exists() else "")

    # Copy tools.md from meta or entity (toolhub-based tool list)
    for tools_md_src in (meta_core_dir / "tools.md", entity_dir / "tools.md"):
        if tools_md_src.exists():
            _write_if_absent(core_dir / "tools.md", tools_md_src.read_text(encoding="utf-8"))
            break

    # Copy skills.md from meta or entity (skillhub-based skill list)
    for skills_md_src in (meta_core_dir / "skills.md", entity_dir / "skills.md"):
        if skills_md_src.exists():
            _write_if_absent(core_dir / "skills.md", skills_md_src.read_text(encoding="utf-8"))
            break

    # Copy config.yaml from meta (or entity) into session core/.
    meta_config_path = meta_core_dir / "config.yaml"
    session_config_path = core_dir / "config.yaml"
    if not session_config_path.exists():
        if meta_config_path.exists():
            shutil.copy2(meta_config_path, session_config_path)
        else:
            # No meta config yet — bootstrap from entity config.yaml
            entity_config_path = entity_dir / "config.yaml"
            if entity_config_path.exists():
                shutil.copy2(entity_config_path, session_config_path)
            else:
                ensure_config(session_dir)
    # Record meta version in status.json so staleness can be detected later.
    meta_version = get_meta_version(entity_name, sys_base=sys_base)
    if meta_version:
        write_session_status(system_dir, agent_version=meta_version)
    # Seed mutable state from meta session, with entity memory as bootstrap fallback.
    sync_from_entity(entity_name, ent_base, s_base=s_base)

    meta_memory = meta_dir / "core" / "memory.md"
    entity_memory = (ent_base / entity_name / "memory.md") if entity_dir.exists() else None
    if meta_memory.exists() and _is_real_memory(meta_memory.read_text(encoding="utf-8")):
        _write_if_absent(core_dir / "memory.md", meta_memory.read_text(encoding="utf-8"))
    elif entity_memory and entity_memory.exists() and _is_real_memory(entity_memory.read_text(encoding="utf-8")):
        _write_if_absent(core_dir / "memory.md", entity_memory.read_text(encoding="utf-8"))
    else:
        _write_if_absent(core_dir / "memory.md", "")

    # Seed layered memory from <entity>_meta/core/memory/ first, then entity/<entity>/memory/.
    memory_seed_dirs = [src_dir for src_dir in (meta_dir / "core" / "memory", ent_base / entity_name / "memory") if src_dir.is_dir()]
    seed_files = [f for src_dir in memory_seed_dirs for f in sorted(src_dir.glob("*.md"))]
    if seed_files:
        session_memory_dir = core_dir / "memory"
        session_memory_dir.mkdir(exist_ok=True)
        for src_file in seed_files:
            dst_file = session_memory_dir / src_file.name
            if not dst_file.exists() and _is_real_memory(src_file.read_text(encoding="utf-8")):
                shutil.copy2(src_file, dst_file)

    # Seed shared playground files from meta-session without overwriting session-local files.
    meta_playground_dir = meta_dir / "playground"
    if meta_playground_dir.is_dir():
        session_playground_dir = session_dir / "playground"
        for src_path in sorted(meta_playground_dir.rglob("*")):
            if src_path.is_dir():
                continue
            rel = src_path.relative_to(meta_playground_dir)
            dst_path = session_playground_dir / rel
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            if not dst_path.exists():
                shutil.copy2(src_path, dst_path)

    # Create task cards directory; seed duty card if config defines one
    tasks_dir = core_dir / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    session_cfg = read_config(session_dir)
    duty = session_cfg.get("duty")
    if isinstance(duty, dict) and duty.get("interval"):
        ensure_card(
            tasks_dir,
            name="duty",
            interval=float(duty["interval"]),
            description=duty.get("description", ""),
        )

    ensure_session_status(system_dir)

    # Write optional initial message
    if initial_message:
        import uuid
        event = {
            "type": "user_input",
            "content": initial_message,
            "id": str(uuid.uuid4()),
            "ts": datetime.now().isoformat(),
        }
        with context_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    return session_id
