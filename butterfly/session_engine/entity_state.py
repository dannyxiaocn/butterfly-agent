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
    for fname in ('system.md', 'task.md', 'env.md', 'memory.md', 'config.yaml'):
        (core_dir / fname).touch(exist_ok=True)
    (core_dir / 'tasks').mkdir(exist_ok=True)
    _create_meta_venv(session_dir)
    return session_dir


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
    from butterfly.session_engine.session_status import read_session_status, write_session_status

    entity_root = entity_base or (_REPO_ROOT / 'entity')
    version = "1.0.0"

    config_yaml = entity_root / entity_name / 'config.yaml'
    if config_yaml.exists():
        try:
            import yaml
            manifest = yaml.safe_load(config_yaml.read_text(encoding='utf-8')) or {}
            v = manifest.get('version')
            if v:
                version = str(v)
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
    from butterfly.session_engine.session_status import read_session_status

    system_base = sys_base or _SYSTEM_SESSIONS_DIR
    system_dir = system_base / get_meta_session_id(entity_name)
    if not (system_dir / "status.json").exists():
        return None
    try:
        status = read_session_status(system_dir)
        return status.get('agent_version')
    except Exception:
        return None


def bump_meta_version(
    entity_name: str,
    note: str = "",
    sys_base: Path | None = None,
) -> str:
    """Increment meta session's agent_version.

    Returns the new version string. Called by the meta agent when the meta
    session's core content is meaningfully updated.
    """
    from butterfly.session_engine.session_status import write_session_status

    current = get_meta_version(entity_name, sys_base=sys_base) or "1.0.0"
    new_version = _increment_version(current)

    system_base = sys_base or _SYSTEM_SESSIONS_DIR
    system_dir = system_base / get_meta_session_id(entity_name)
    try:
        write_session_status(system_dir, agent_version=new_version)
    except Exception:
        pass
    return new_version


# ── Meta session bootstrap ────────────────────────────────────────────────────

def populate_meta_from_entity(
    entity_name: str,
    entity_base: Path | None = None,
    s_base: Path | None = None,
    sys_base: Path | None = None,
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
        ('prompts/task.md', 'task.md'),
        ('prompts/env.md', 'env.md'),
    ]:
        src = entity_dir / src_name
        if src.exists():
            (core_dir / dst_name).write_text(src.read_text(encoding='utf-8'), encoding='utf-8')

    # Copy tools.md (toolhub-based tool list), fallback to legacy tool.md
    tools_md = entity_dir / 'tools.md'
    legacy_tool_md = entity_dir / 'tool.md'
    src_tools_md = tools_md if tools_md.exists() else legacy_tool_md
    if src_tools_md.exists():
        (core_dir / 'tools.md').write_text(src_tools_md.read_text(encoding='utf-8'), encoding='utf-8')

    # Copy skills.md (skillhub-based skill list)
    skills_md = entity_dir / 'skills.md'
    if skills_md.exists():
        (core_dir / 'skills.md').write_text(skills_md.read_text(encoding='utf-8'), encoding='utf-8')

    # Copy entity's config.yaml into meta session core/.
    # config.yaml contains model, provider, thinking, prompts.
    # start_meta_agent() ensures basic config exists after this call.
    entity_config = entity_dir / 'config.yaml'
    if entity_config.exists():
        shutil.copy2(entity_config, core_dir / 'config.yaml')

    # Seed version from entity (writes to _sessions/<entity>_meta/status.json)
    _init_meta_version(entity_name, entity_base, sys_base=sys_base)


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
    """Read the ``gene`` list from the entity's config.yaml."""
    entity_root = entity_base or (_REPO_ROOT / 'entity')
    entity_dir = entity_root / entity_name

    yaml_path = entity_dir / 'config.yaml'
    if not yaml_path.exists():
        return []
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
    """Execute the ``gene`` shell commands from config.yaml in the meta playground."""
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
1. Bump the version in _sessions/{entity}_meta/status.json (e.g. "1.0.0" → "1.0.1")
2. Open a PR to sync changes back to entity/{entity}/

## Updating Entity Definition
All entity updates go through the single branch `mecam/entity-update`.
This keeps the entity/ directory as a stable, reviewable snapshot of your current state.

You have access to bash to inspect sessions, read their content, and manage child sessions.
Meta-session memory is system-managed; keep it current through your task cycles.

# TODO: more efficient tools for learning what to update (e.g. session diff summaries, structured change detection)
"""

_META_HEARTBEAT_PROMPT = """Dream cycle: Review all child sessions and maintain entity health.

## 1. Review child sessions
List: `butterfly sessions --json` (filter by entity from _sessions/*/manifest.json)
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
If you updated core/ files (system.md, task.md, tools.md, skills.md):
a. Bump version — update "agent_version" in _sessions/{entity}_meta/status.json
b. Create PR to mecam/entity-update branch:
   ```bash
   cd <repo_root>
   git checkout -B mecam/entity-update
   cp sessions/{entity}_meta/core/system.md entity/{entity}/prompts/system.md
   cp sessions/{entity}_meta/core/task.md entity/{entity}/prompts/task.md
   cp sessions/{entity}_meta/core/env.md entity/{entity}/prompts/env.md
   cp sessions/{entity}_meta/core/tools.md entity/{entity}/tools.md
   cp sessions/{entity}_meta/core/skills.md entity/{entity}/skills.md
   # Update version in entity/{entity}/config.yaml
   git add entity/{entity}/
   git commit -m "meta: update entity {entity} vX.Y.Z"
   gh pr create --title "Entity update: {entity} vX.Y.Z" --base main --head mecam/entity-update --body "Automated update from meta-agent dream cycle."
   ```

Be intelligent — consider context and importance, not just mechanical rules.

# TODO: more efficient tools for learning what to update (session diff summaries, change detection)
"""

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
    from butterfly.session_engine.session_status import ensure_session_status
    from butterfly.session_engine.session_config import ensure_config
    from butterfly.session_engine.task_cards import ensure_card

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

    task_md = core_dir / "task.md"
    if not task_md.read_text(encoding="utf-8").strip():
        task_md.write_text(
            _META_HEARTBEAT_PROMPT.format(entity=entity_name).strip() + "\n",
            encoding="utf-8",
        )

    # Ensure basic config exists
    ensure_config(meta_dir)

    # Create meta task card (6-hour recurring cycle)
    ensure_card(
        core_dir / "tasks",
        name="meta",
        interval=21600.0,
        description="Dream: review and process all child sessions for this entity",
    )
    return system_dir



# ── Future work (TODOs) ───────────────────────────────────────────────────────

# TODO: When user updates entity/, the meta session should have an "update from entity"
# workflow that merges the entity's changes with the meta session's own accumulated
# changes — rather than overwriting either side blindly.

# TODO: Normal sessions could optionally include an "update agent core" capability,
# letting users promote useful session-level improvements back into the meta session.
# Should be user-triggered, not automatic, to avoid polluting the meta session.
