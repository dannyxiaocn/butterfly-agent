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

from nutshell.session_engine.session_params import ensure_session_params, write_session_params
from nutshell.session_engine.session_status import ensure_session_status, write_session_status
from nutshell.session_engine.task_cards import ensure_heartbeat_card
from nutshell.session_engine.entity_state import (
    _meta_is_synced,
    check_meta_alignment,
    ensure_gene_initialized,
    ensure_meta_session,
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


def _load_entity_params(entity_dir: Path) -> dict:
    """Read the ``params`` mapping from an entity's agent.yaml (if any).

    Returns a dict of param overrides (e.g. session_type, heartbeat_task,
    heartbeat_interval) that should be written into the session's params.json.
    Converts legacy ``persistent: true`` to ``session_type: "persistent"``.
    """
    yaml_path = entity_dir / "agent.yaml"
    if not yaml_path.exists():
        return {}
    try:
        import yaml
        manifest = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    params = dict(manifest.get("params") or {})
    # Backward compat: convert persistent bool → session_type
    if "persistent" in params and "session_type" not in params:
        if params.pop("persistent"):
            params["session_type"] = "persistent"
        else:
            params.pop("persistent")
    if "default_task" in params and "heartbeat_task" not in params:
        params["heartbeat_task"] = params["default_task"]
    return params

def init_session(
    session_id: str,
    entity_name: str,
    *,
    sessions_base: Path | None = None,
    system_sessions_base: Path | None = None,
    entity_base: Path | None = None,
    heartbeat: float = 600.0,
    initial_message: str | None = None,
) -> str:
    """Create a new session on disk from an entity, ready for the server to pick up.

    Returns the session_id. Idempotent: only writes files that do not exist yet.

    Args:
        session_id:          The unique session identifier (e.g. '2026-03-25_10-00-00').
        entity_name:         Name of the entity in entity_base/ (e.g. 'agent', 'nutshell_dev').
        sessions_base:       Root of agent-visible sessions/ directory.
        system_sessions_base: Root of _sessions/ directory.
        entity_base:         Root of entity/ directory.
        heartbeat:           Heartbeat interval in seconds (default: 600).
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

    # Load entity-level param overrides (persistent, heartbeat task seed, etc.)
    entity_params = _load_entity_params(entity_dir)
    effective_heartbeat = entity_params.pop("heartbeat_interval", None) or heartbeat
    heartbeat_task_content = entity_params.pop("heartbeat_task", None)
    entity_params.pop("default_task", None)

    # Config always comes from meta session; meta is initially populated from entity.
    meta_dir = ensure_meta_session(entity_name, s_base=s_base)
    if entity_dir.exists():
        if not _meta_is_synced(meta_dir):
            populate_meta_from_entity(entity_name, ent_base, s_base)
        else:
            check_meta_alignment(entity_name, ent_base, s_base)
        ensure_gene_initialized(entity_name, ent_base, s_base)
        start_meta_agent(entity_name, entity_base=ent_base, s_base=s_base, sys_base=sys_base)

    meta_core_dir = meta_dir / "core"
    for fname in ("system.md", "heartbeat.md", "session.md"):
        src = meta_core_dir / fname
        _write_if_absent(core_dir / fname, src.read_text(encoding="utf-8") if src.exists() else "")

    meta_tools_dir = meta_core_dir / "tools"
    if meta_tools_dir.is_dir():
        for src in sorted(meta_tools_dir.glob('*.json')):
            dst = core_dir / "tools" / src.name
            if not dst.exists():
                shutil.copy2(src, dst)

    meta_skills_dir = meta_core_dir / "skills"
    if meta_skills_dir.is_dir():
        for src in sorted(meta_skills_dir.rglob('*')):
            rel = src.relative_to(meta_skills_dir)
            dst = core_dir / "skills" / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.exists():
                    shutil.copy2(src, dst)

    if not (core_dir / "params.json").exists():
        try:
            import yaml
            manifest = yaml.safe_load((entity_dir / 'agent.yaml').read_text(encoding='utf-8')) if (entity_dir / 'agent.yaml').exists() else {}
        except Exception:
            manifest = {}
        extra: dict = {}
        if manifest.get('fallback_model'):
            extra['fallback_model'] = manifest['fallback_model']
        if manifest.get('fallback_provider'):
            extra['fallback_provider'] = manifest['fallback_provider']
        model = manifest.get('model')
        provider = manifest.get('provider') or 'anthropic'
        write_session_params(session_dir, heartbeat_interval=effective_heartbeat, model=model, provider=provider, **extra, **entity_params)
    else:
        write_session_params(session_dir, heartbeat_interval=effective_heartbeat, **entity_params)
    # Seed mutable state from meta session, with entity memory as bootstrap fallback.
    meta_dir = ensure_meta_session(entity_name, s_base=s_base)
    sync_from_entity(entity_name, ent_base, s_base=s_base)

    meta_memory = meta_dir / "core" / "memory.md"
    entity_memory = (ent_base / entity_name / "memory.md") if entity_dir.exists() else None
    if meta_memory.exists() and meta_memory.read_text(encoding="utf-8"):
        _write_if_absent(core_dir / "memory.md", meta_memory.read_text(encoding="utf-8"))
    elif entity_memory and entity_memory.exists():
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
            if not dst_file.exists():
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

    # Create task cards directory; seed heartbeat card for persistent sessions
    tasks_dir = core_dir / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    if entity_params.get("session_type") == "persistent":
        ensure_heartbeat_card(
            tasks_dir,
            interval=effective_heartbeat,
            content=heartbeat_task_content,
        )

    ensure_session_status(system_dir)
    write_session_status(system_dir, heartbeat_interval=effective_heartbeat)

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
