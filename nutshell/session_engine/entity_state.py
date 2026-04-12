from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SESSIONS_DIR = _REPO_ROOT / 'sessions'
_SYSTEM_SESSIONS_DIR = _REPO_ROOT / '_sessions'


def get_meta_session_id(entity_name: str) -> str:
    return f"{entity_name}_meta"


def get_meta_dir(entity_name: str, s_base: Path | None = None) -> Path:
    return (s_base or _SESSIONS_DIR) / get_meta_session_id(entity_name)


def _create_meta_venv(meta_dir: Path) -> Path:
    """Create a Python venv at meta_dir/.venv (idempotent)."""
    venv_path = meta_dir / '.venv'
    if venv_path.exists():
        return venv_path
    subprocess.run(
        [sys.executable, '-m', 'venv', '--system-site-packages', str(venv_path)],
        check=True,
        capture_output=True,
    )
    return venv_path


def ensure_meta_session(entity_name: str, s_base: Path | None = None) -> Path:
    """Create sessions/<entity>_meta/ directory structure (idempotent)."""
    session_dir = get_meta_dir(entity_name, s_base=s_base)
    core_dir = session_dir / 'core'
    core_dir.mkdir(parents=True, exist_ok=True)
    (core_dir / 'tools').mkdir(exist_ok=True)
    (core_dir / 'skills').mkdir(exist_ok=True)
    (core_dir / 'memory').mkdir(exist_ok=True)
    (session_dir / 'docs').mkdir(exist_ok=True)
    (session_dir / 'playground').mkdir(exist_ok=True)
    for fname in ('system.md', 'heartbeat.md', 'session.md', 'memory.md', 'config.yaml'):
        (core_dir / fname).touch(exist_ok=True)
    (core_dir / 'tasks').mkdir(exist_ok=True)
    _create_meta_venv(session_dir)
    return session_dir


def _meta_is_synced(meta_dir: Path) -> bool:
    return (meta_dir / 'core' / '.entity_synced').exists()


def _mark_meta_synced(meta_dir: Path, entity_name: str) -> None:
    (meta_dir / 'core' / '.entity_synced').write_text(entity_name, encoding='utf-8')


def _clear_dir_contents(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


# ── Version management ────────────────────────────────────────────────────────

def _increment_version(version: str) -> str:
    """Increment patch version: "1.0.0" → "1.0.1"."""
    parts = version.split('.')
    try:
        parts[-1] = str(int(parts[-1]) + 1)
        return '.'.join(parts)
    except (ValueError, IndexError):
        return version + ".1"


def _init_meta_version(
    entity_name: str,
    entity_base: Path | None = None,
    sys_base: Path | None = None,
) -> None:
    """Seed agent_version in status.json from entity's config.yaml (first-time only)."""
    from nutshell.session_engine.session_status import read_session_status, write_session_status

    entity_root = entity_base or (_REPO_ROOT / 'entity')
    version = "1.0.0"

    # Try config.yaml first, fall back to legacy agent.yaml
    config_yaml = entity_root / entity_name / 'config.yaml'
    agent_yaml = entity_root / entity_name / 'agent.yaml'
    for yaml_path in (config_yaml, agent_yaml):
        if yaml_path.exists():
            try:
                import yaml
                manifest = yaml.safe_load(yaml_path.read_text(encoding='utf-8')) or {}
                v = manifest.get('version')
                if v:
                    version = str(v)
                    break
            except Exception:
                pass

    system_base = sys_base or _SYSTEM_SESSIONS_DIR
    system_dir = system_base / get_meta_session_id(entity_name)
    try:
        status = read_session_status(system_dir)
        if 'agent_version' not in status or status.get('agent_version') is None:
            write_session_status(system_dir, agent_version=version)
    except Exception:
        pass


def get_meta_version(entity_name: str, sys_base: Path | None = None) -> str | None:
    """Return the current agent_version from status.json, or None."""
    from nutshell.session_engine.session_status import read_session_status

    system_base = sys_base or _SYSTEM_SESSIONS_DIR
    system_dir = system_base / get_meta_session_id(entity_name)
    if not (system_dir / "status.json").exists():
        return None
    try:
        status = read_session_status(system_dir)
        return status.get('agent_version')
    except Exception:
        return None


def _record_version_entry(
    entity_name: str,
    version: str,
    note: str = "",
    sys_base: Path | None = None,
) -> None:
    """Append a version entry to _sessions/<entity>_meta/version_history.json."""
    system_base = sys_base or _SYSTEM_SESSIONS_DIR
    history_path = system_base / f"{entity_name}_meta" / "version_history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history: list[dict] = []
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text(encoding='utf-8'))
        except Exception:
            history = []
    history.append({"version": version, "ts": datetime.now().isoformat(), "note": note})
    history_path.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding='utf-8')


def bump_meta_version(
    entity_name: str,
    note: str = "",
    sys_base: Path | None = None,
) -> str:
    """Increment meta session's agent_version, record in version history.

    Returns the new version string. Called by the meta agent or CLI when
    the meta session's core content is meaningfully updated.
    """
    from nutshell.session_engine.session_status import write_session_status

    current = get_meta_version(entity_name, sys_base=sys_base) or "1.0.0"
    new_version = _increment_version(current)

    system_base = sys_base or _SYSTEM_SESSIONS_DIR
    system_dir = system_base / get_meta_session_id(entity_name)
    try:
        write_session_status(system_dir, agent_version=new_version)
        _record_version_entry(entity_name, new_version, note, sys_base=sys_base)
    except Exception:
        pass
    return new_version


def get_version_history(entity_name: str, sys_base: Path | None = None) -> list[dict]:
    """Return version history list from _sessions/<entity>_meta/version_history.json."""
    system_base = sys_base or _SYSTEM_SESSIONS_DIR
    history_path = system_base / f"{entity_name}_meta" / "version_history.json"
    if not history_path.exists():
        return []
    try:
        return json.loads(history_path.read_text(encoding='utf-8'))
    except Exception:
        return []


# ── Meta session bootstrap ────────────────────────────────────────────────────

def populate_meta_from_entity(
    entity_name: str,
    entity_base: Path | None = None,
    s_base: Path | None = None,
) -> None:
    """Copy entity content into meta session core. Called once at meta creation.

    Entity is the initial seed for the meta session. After this call the meta
    session is self-contained and evolves independently. Entity and meta session
    are not kept in sync automatically — the meta agent submits PRs to update
    the entity via the mecam/entity-update branch.
    """
    entity_root = entity_base or (_REPO_ROOT / 'entity')
    entity_dir = entity_root / entity_name
    if not entity_dir.exists():
        return

    meta_dir = ensure_meta_session(entity_name, s_base=s_base)
    core_dir = meta_dir / 'core'

    # Copy prompts
    for src_name, dst_name in [
        ('prompts/system.md', 'system.md'),
        ('prompts/heartbeat.md', 'heartbeat.md'),
        ('prompts/session.md', 'session.md'),
    ]:
        src = entity_dir / src_name
        if src.exists():
            (core_dir / dst_name).write_text(src.read_text(encoding='utf-8'), encoding='utf-8')

    # Copy tools
    dst_tools = core_dir / 'tools'
    _clear_dir_contents(dst_tools)
    src_tools = entity_dir / 'tools'
    if src_tools.is_dir():
        for src in sorted(src_tools.glob('*.json')):
            shutil.copy2(src, dst_tools / src.name)

    # Copy skills
    dst_skills = core_dir / 'skills'
    _clear_dir_contents(dst_skills)
    src_skills = entity_dir / 'skills'
    if src_skills.is_dir():
        for src in sorted(src_skills.rglob('*')):
            rel = src.relative_to(src_skills)
            dst = dst_skills / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    # Copy entity's config.yaml into meta session core/.
    # config.yaml already contains everything (model, provider, thinking, tools, skills, prompts).
    # Runtime defaults (heartbeat_interval, session_type, etc.) are filled in
    # by start_meta_agent() via _META_AGENT_DEFAULTS after this call.
    entity_config = entity_dir / 'config.yaml'
    if entity_config.exists():
        shutil.copy2(entity_config, core_dir / 'config.yaml')

    # Seed version from entity (writes to _sessions/<entity>_meta/status.json)
    _init_meta_version(entity_name, entity_base)

    _mark_meta_synced(meta_dir, entity_name)


def sync_from_entity(entity_name: str, entity_base: Path | None = None, s_base: Path | None = None) -> None:
    """Bootstrap meta-session mutable state (memory, playground) from entity on first use.

    Existing meta-session files are preserved (only missing files are seeded).
    """
    entity_root = entity_base or (_REPO_ROOT / 'entity')
    entity_dir = entity_root / entity_name
    meta_dir = ensure_meta_session(entity_name, s_base=s_base)
    core_dir = meta_dir / 'core'
    meta_memory = core_dir / 'memory.md'

    entity_memory = entity_dir / 'memory.md'
    meta_memory_text = meta_memory.read_text(encoding='utf-8') if meta_memory.exists() else ''
    if entity_memory.exists() and not meta_memory_text:
        meta_memory.write_text(entity_memory.read_text(encoding='utf-8'), encoding='utf-8')

    entity_memory_dir = entity_dir / 'memory'
    if entity_memory_dir.is_dir():
        meta_memory_dir = core_dir / 'memory'
        meta_memory_dir.mkdir(parents=True, exist_ok=True)
        for src_file in sorted(entity_memory_dir.glob('*.md')):
            dst_file = meta_memory_dir / src_file.name
            if not dst_file.exists():
                shutil.copy2(src_file, dst_file)

    entity_playground_dir = entity_dir / 'playground'
    if entity_playground_dir.is_dir():
        meta_playground_dir = meta_dir / 'playground'
        meta_playground_dir.mkdir(parents=True, exist_ok=True)
        for src_path in sorted(entity_playground_dir.rglob('*')):
            if src_path.is_dir():
                continue
            rel = src_path.relative_to(entity_playground_dir)
            dst_path = meta_playground_dir / rel
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            if not dst_path.exists():
                shutil.copy2(src_path, dst_path)


def _load_gene_commands(entity_name: str, entity_base: Path | None = None) -> list[str]:
    """Read the ``gene`` list from the entity's config.yaml (falls back to agent.yaml)."""
    entity_root = entity_base or (_REPO_ROOT / 'entity')
    entity_dir = entity_root / entity_name

    # Try config.yaml first, fall back to legacy agent.yaml
    for fname in ('config.yaml', 'agent.yaml'):
        yaml_path = entity_dir / fname
        if not yaml_path.exists():
            continue
        try:
            import yaml
            manifest = yaml.safe_load(yaml_path.read_text(encoding='utf-8')) or {}
            gene = manifest.get('gene')
            if gene and isinstance(gene, list):
                return [str(cmd) for cmd in gene]
        except Exception:
            pass
    return []


def run_gene_commands(
    entity_name: str,
    entity_base: Path | None = None,
    s_base: Path | None = None,
) -> None:
    """Execute the ``gene`` shell commands from agent.yaml in the meta playground."""
    commands = _load_gene_commands(entity_name, entity_base)
    if not commands:
        return

    meta_dir = ensure_meta_session(entity_name, s_base=s_base)
    playground_dir = meta_dir / 'playground'
    playground_dir.mkdir(parents=True, exist_ok=True)

    import os
    venv_path = _create_meta_venv(meta_dir)
    env = os.environ.copy()
    env['VIRTUAL_ENV'] = str(venv_path)
    venv_bin = venv_path / 'bin'
    env['PATH'] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
    env.pop('PYTHONHOME', None)

    for cmd in commands:
        print(f"[gene] Running: {cmd}")
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=str(playground_dir),
                env=env,
                capture_output=True,
                text=True,
            )
            if result.stdout:
                print(result.stdout, end='')
            if result.returncode != 0:
                print(f"[gene] Command failed (exit {result.returncode}): {cmd}")
                if result.stderr:
                    print(result.stderr, end='')
        except Exception as exc:
            print(f"[gene] Error running '{cmd}': {exc}")

    marker = meta_dir / 'core' / '.gene_initialized'
    marker.write_text(entity_name, encoding='utf-8')
    print(f"[gene] Initialized for entity '{entity_name}'")


def ensure_gene_initialized(
    entity_name: str,
    entity_base: Path | None = None,
    s_base: Path | None = None,
) -> None:
    """Run gene commands only if ``core/.gene_initialized`` marker is absent."""
    meta_dir = get_meta_dir(entity_name, s_base=s_base)
    marker = meta_dir / 'core' / '.gene_initialized'
    if marker.exists():
        return
    run_gene_commands(entity_name, entity_base=entity_base, s_base=s_base)


# ── Meta Agent ────────────────────────────────────────────────────────────────

_META_SYSTEM_PROMPT = """You are the meta-agent for entity '{entity}'. You are the authoritative, living version of this entity's configuration.

## Your Role
- Your core/ directory IS the current configuration for entity '{entity}'
- All new child sessions are seeded from your core/
- Child sessions running older versions receive automatic update notices
- You evolve independently — entity/{entity}/ is downstream of you

## Version Management
Your version is stored in _sessions/{entity}_meta/status.json as "agent_version".
When you make meaningful improvements to core/:
1. Use the CLI or bump the version in _sessions/{entity}_meta/status.json (e.g. "1.0.0" → "1.0.1")
2. Append to _sessions/{entity}_meta/version_history.json:
   {{"version": "X.Y.Z", "ts": "<ISO timestamp>", "note": "what changed"}}
3. Open a PR to sync changes back to entity/{entity}/ (see heartbeat for steps)

## Updating Entity Definition
All entity updates go through the single branch `mecam/entity-update`.
This keeps the entity/ directory as a stable, reviewable snapshot of your current state.

You have access to bash to inspect sessions, read their content, and manage child sessions.
Meta-session memory is system-managed; keep it current through your heartbeat cycle.

# TODO: more efficient tools for learning what to update (e.g. session diff summaries, structured change detection)
"""

_META_HEARTBEAT_PROMPT = """Dream cycle: Review all child sessions and maintain entity health.

## 1. Review child sessions
List: `nutshell sessions --json` (filter by entity from _sessions/*/manifest.json)
For each session decide:
- Active / has pending tasks → keep, note in memory
- Completed with learnings → extract key info to meta memory, then delete
- Old / empty / trivial → delete directly

Safety: NEVER delete sessions running or active < 2 hours ago

## 2. Update meta memory
Keep core/memory.md accurate:
- Active sessions (id + purpose + status)
- Key learnings from archived sessions

## 3. Sync core updates back to entity (if you improved anything)
If you updated core/ files (system.md, heartbeat.md, tools/, skills/):
a. Bump version — update "agent_version" in _sessions/{entity}_meta/status.json
b. Record — append to _sessions/{entity}_meta/version_history.json:
   {{"version":"X.Y.Z","ts":"<ISO>","note":"<what changed>"}}
c. Create PR to mecam/entity-update branch:
   ```bash
   cd <repo_root>
   git checkout -B mecam/entity-update
   cp sessions/{entity}_meta/core/system.md entity/{entity}/prompts/system.md
   cp sessions/{entity}_meta/core/heartbeat.md entity/{entity}/prompts/heartbeat.md
   cp sessions/{entity}_meta/core/session.md entity/{entity}/prompts/session.md
   cp sessions/{entity}_meta/core/tools/*.json entity/{entity}/tools/
   # Update version in entity/{entity}/config.yaml
   git add entity/{entity}/
   git commit -m "meta: update entity {entity} vX.Y.Z"
   gh pr create --title "Entity update: {entity} vX.Y.Z" --base main --head mecam/entity-update --body "Automated update from meta-agent dream cycle."
   ```

Be intelligent — consider context and importance, not just mechanical rules.

# TODO: more efficient tools for learning what to update (session diff summaries, change detection)
"""

_META_AGENT_DEFAULTS = {
    "session_type": "persistent",
    "heartbeat_interval": 21600,
    "default_task": "Dream: review and process all child sessions for this entity",
}


def start_meta_agent(
    entity_name: str,
    entity_base: Path | None = None,
    s_base: Path | None = None,
    sys_base: Path | None = None,
) -> Path:
    """Ensure meta session has a _sessions/ system dir so the watcher starts it as an agent.

    Idempotent — safe to call multiple times.
    Returns the system dir path (_sessions/<entity>_meta/).
    """
    from nutshell.session_engine.session_status import ensure_session_status
    from nutshell.session_engine.session_config import read_config, write_config
    from nutshell.session_engine.task_cards import ensure_heartbeat_card, migrate_legacy_default_task

    sessions_base = s_base or _SESSIONS_DIR
    system_base = sys_base or _SYSTEM_SESSIONS_DIR

    meta_id = get_meta_session_id(entity_name)
    meta_dir = ensure_meta_session(entity_name, s_base=sessions_base)
    system_dir = system_base / meta_id

    system_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "session_id": meta_id,
        "entity": entity_name,
        "created_at": datetime.now().isoformat(),
    }
    manifest_path = system_dir / "manifest.json"
    if not manifest_path.exists():
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    (system_dir / "context.jsonl").touch(exist_ok=True)
    (system_dir / "events.jsonl").touch(exist_ok=True)

    ensure_session_status(system_dir)

    core_dir = meta_dir / "core"

    system_md = core_dir / "system.md"
    if not system_md.read_text(encoding="utf-8").strip():
        system_md.write_text(
            _META_SYSTEM_PROMPT.format(entity=entity_name).strip() + "\n",
            encoding="utf-8",
        )

    heartbeat_md = core_dir / "heartbeat.md"
    if not heartbeat_md.read_text(encoding="utf-8").strip():
        heartbeat_md.write_text(
            _META_HEARTBEAT_PROMPT.format(entity=entity_name).strip() + "\n",
            encoding="utf-8",
        )

    current_config = read_config(meta_dir)
    updates: dict = {}
    for key, default_val in _META_AGENT_DEFAULTS.items():
        if current_config.get(key) in (None, False, 0, ""):
            updates[key] = default_val
    if updates:
        write_config(meta_dir, **updates)
    config_after = read_config(meta_dir)
    ensure_heartbeat_card(
        core_dir / "tasks",
        interval=float(config_after.get("heartbeat_interval") or _META_AGENT_DEFAULTS["heartbeat_interval"]),
        content=config_after.get("default_task"),
    )
    migrate_legacy_default_task(meta_dir)

    return system_dir



# ── Future work (TODOs) ───────────────────────────────────────────────────────

# TODO: When user updates entity/, the meta session should have an "update from entity"
# workflow that merges the entity's changes with the meta session's own accumulated
# changes — rather than overwriting either side blindly.

# TODO: Normal sessions could optionally include an "update agent core" capability,
# letting users promote useful session-level improvements back into the meta session.
# Should be user-triggered, not automatic, to avoid polluting the meta session.
