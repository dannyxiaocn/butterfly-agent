"""Shared session initialization — creates session directory structure from an entity.

Used by:
  - butterfly/service/sessions_service.py  (web UI new-session endpoint)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from butterfly.session_engine.session_config import read_config, write_config, ensure_config
from butterfly.session_engine.session_status import ensure_session_status, write_session_status
from butterfly.session_engine.task_cards import ensure_card
from butterfly.session_engine.entity_state import (
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
) -> str:
    """Create a new session on disk from an entity, ready for the server to pick up.

    Returns the session_id. Idempotent: only writes files that do not exist yet.

    Args:
        session_id:          The unique session identifier (e.g. '2026-03-25_10-00-00').
        entity_name:         Name of the entity in entity_base/ (e.g. 'agent', 'butterfly_dev').
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

    # NOTE (first-run race fix — see docs/butterfly/session_engine/design.md):
    # manifest.json is the watcher's discovery signal. It MUST be written LAST —
    # after sessions/<id>/core/config.yaml is populated with the entity's real
    # model and provider. If we publish manifest.json first, the server-side
    # watcher races us: it spawns Session(session_id) whose Session.__init__
    # calls ensure_config(session_dir), which writes DEFAULT_CONFIG
    # (model=None, provider=None) before init_session's config copy runs.
    # Our `if not session_config_path.exists()` guard then skips the real
    # copy, leaving model=null persisted on disk. The manifest write is
    # deferred to the end of this function for that reason.
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
    # Copy prompts
    for name in ("system.md", "task.md", "env.md"):
        src = meta_core_dir / name
        _write_if_absent(core_dir / name, src.read_text(encoding="utf-8") if src.exists() else "")

    # Copy tools.md from meta or entity (toolhub-based tool list), fallback to legacy tool.md
    for tools_md_src in (meta_core_dir / "tools.md", entity_dir / "tools.md",
                         meta_core_dir / "tool.md", entity_dir / "tool.md"):
        if tools_md_src.exists():
            _write_if_absent(core_dir / "tools.md", tools_md_src.read_text(encoding="utf-8"))
            break

    # Copy skills.md from meta or entity (skillhub-based skill list)
    for skills_md_src in (meta_core_dir / "skills.md", entity_dir / "skills.md"):
        if skills_md_src.exists():
            _write_if_absent(core_dir / "skills.md", skills_md_src.read_text(encoding="utf-8"))
            break

    # Copy config.yaml from meta (or entity) into session core/.
    #
    # If a stub config.yaml already exists (e.g. a racing Session.__init__
    # called ensure_config() and wrote DEFAULT_CONFIG before we got here),
    # we still need to seed model/provider from the entity — otherwise the
    # session ships with `model: null` and the agent has no model to run
    # (v2.0.8 first-run bug).
    meta_config_path = meta_core_dir / "config.yaml"
    session_config_path = core_dir / "config.yaml"

    def _needs_seed(path: Path) -> bool:
        if not path.exists():
            return True
        try:
            import yaml as _yaml
            loaded = _yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            return True
        # safe_load returns None for empty files, and may return lists/scalars
        # for malformed-but-valid YAML. Anything that isn't a mapping is
        # treated as "needs seed" — safer to overwrite a broken stub with the
        # real entity config than to trust it.
        if not isinstance(loaded, dict):
            return True
        # Stub DEFAULT_CONFIG written by ensure_config has model=None and
        # provider=None; real entity configs always carry both. If either
        # is missing/falsy, re-seed from the entity.
        return not loaded.get("model") or not loaded.get("provider")

    if _needs_seed(session_config_path):
        if meta_config_path.exists() and meta_config_path.read_text(encoding="utf-8").strip():
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

    # Create panel directory (non-blocking tool state + future sub-agent refs)
    (core_dir / "panel").mkdir(parents=True, exist_ok=True)
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

    # Publish manifest LAST (see NOTE above about watcher race):
    # by the time manifest.json is visible to the watcher, sessions/<id>/core/
    # has a fully-populated config.yaml, so Session.__init__'s ensure_config
    # is a no-op instead of clobbering model/provider with DEFAULT_CONFIG.
    manifest = {
        "session_id": session_id,
        "entity": entity_name,
        "created_at": datetime.now().isoformat(),
    }
    (system_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

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
