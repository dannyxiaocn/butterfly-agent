from __future__ import annotations

import difflib
import json
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SESSIONS_DIR = _REPO_ROOT / 'sessions'


class MetaAlignmentError(Exception):
    """meta session config 与 entity 不一致时抛出。"""

    def __init__(self, entity_name: str, diffs: list[dict]):
        self.entity_name = entity_name
        self.diffs = diffs
        super().__init__(f"meta session alignment conflict for entity '{entity_name}' ({len(diffs)} diff(s))")

    def format_report(self) -> str:
        lines = [f"=== ALIGNMENT CONFLICT: {self.entity_name} ===", ""]
        for idx, diff in enumerate(self.diffs):
            path = diff['path']
            entity_label = f"entity/{self.entity_name}/{_entity_rel_from_meta_path(path)}"
            meta_label = f"sessions/{get_meta_session_id(self.entity_name)}/{path}"
            entity_text = diff.get('entity', '')
            meta_text = diff.get('meta', '')
            entity_lines = entity_text.splitlines(keepends=True)
            meta_lines = meta_text.splitlines(keepends=True)
            unified = list(difflib.unified_diff(entity_lines, meta_lines, fromfile=entity_label, tofile=meta_label))
            if unified:
                lines.extend([line.rstrip('\n') for line in unified])
            else:
                lines.append(f"--- {entity_label} (different)")
                lines.append(f"+++ {meta_label} (different)")
                lines.append("(content differs but unified diff is empty)")
            if idx != len(self.diffs) - 1:
                lines.append("")
        return "\n".join(lines)


def get_meta_session_id(entity_name: str) -> str:
    return f"{entity_name}_meta"


def get_meta_dir(entity_name: str, s_base: Path | None = None) -> Path:
    return (s_base or _SESSIONS_DIR) / get_meta_session_id(entity_name)



def _create_meta_venv(meta_dir: Path) -> Path:
    """Create a Python venv at meta_dir/.venv (idempotent).

    Uses --system-site-packages so globally installed packages are available.
    Returns the venv path.
    """
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
    for fname in ('system.md', 'heartbeat.md', 'session.md', 'memory.md'):
        (core_dir / fname).touch(exist_ok=True)
    (core_dir / 'tasks').mkdir(exist_ok=True)
    _create_meta_venv(session_dir)
    return session_dir


def _meta_is_synced(meta_dir: Path) -> bool:
    return (meta_dir / 'core' / '.entity_synced').exists()


def _mark_meta_synced(meta_dir: Path, entity_name: str) -> None:
    (meta_dir / 'core' / '.entity_synced').write_text(entity_name, encoding='utf-8')


def _entity_rel_from_meta_path(path: str) -> str:
    if path == 'core/system.md':
        return 'prompts/system.md'
    if path == 'core/heartbeat.md':
        return 'prompts/heartbeat.md'
    if path == 'core/session.md':
        return 'prompts/session.md'
    if path.startswith('core/tools/'):
        return 'tools/' + path.removeprefix('core/tools/')
    if path.startswith('core/skills/'):
        return 'skills/' + path.removeprefix('core/skills/')
    return path.removeprefix('core/')


def _load_agent_config(entity_name: str, entity_base: Path):
    from nutshell.session_engine.entity_config import AgentConfig

    entity_dir = entity_base / entity_name
    if not entity_dir.exists():
        return None
    try:
        return AgentConfig.from_path(entity_dir)
    except Exception:
        return None


def _inheritance_fields(entity_name: str, entity_base: Path) -> tuple[set[str], set[str]]:
    config = _load_agent_config(entity_name, entity_base)
    if config is None:
        return set(), set()
    own_fields = set(config.inheritance.own)
    inherited_fields = set(config.inheritance.link) | set(config.inheritance.append)
    return own_fields, inherited_fields


def _parent_entity_name(entity_name: str, entity_base: Path) -> str | None:
    config = _load_agent_config(entity_name, entity_base)
    return config.extends if config is not None else None


def _copy_missing_files(src_dir: Path, dst_dir: Path) -> None:
    if not src_dir.is_dir():
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(src_dir.rglob('*')):
        if src.is_dir():
            continue
        rel = src.relative_to(src_dir)
        dst = dst_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            shutil.copy2(src, dst)


def _sync_inherited_memory(entity_name: str, entity_base: Path, core_dir: Path) -> None:
    own_fields, _ = _inheritance_fields(entity_name, entity_base)
    if 'memory' in own_fields or 'memory.md' in own_fields:
        return
    parent_name = _parent_entity_name(entity_name, entity_base)
    if not parent_name:
        return
    parent_dir = entity_base / parent_name
    parent_memory = parent_dir / 'memory.md'
    meta_memory = core_dir / 'memory.md'
    if parent_memory.exists() and not meta_memory.read_text(encoding='utf-8').strip():
        meta_memory.write_text(parent_memory.read_text(encoding='utf-8'), encoding='utf-8')
    _copy_missing_files(parent_dir / 'memory', core_dir / 'memory')


def _sync_inherited_playground(entity_name: str, entity_base: Path, meta_dir: Path) -> None:
    own_fields, _ = _inheritance_fields(entity_name, entity_base)
    if 'playground' in own_fields:
        return
    parent_name = _parent_entity_name(entity_name, entity_base)
    if not parent_name:
        return
    parent_dir = entity_base / parent_name
    _copy_missing_files(parent_dir / 'playground', meta_dir / 'playground')


def _resolve_entity_tools_dir(entity_name: str, entity_base: Path) -> Path | None:
    """Walk the extends chain to find the first entity that has a non-empty tools/ dir."""
    seen: set[str] = set()
    current = entity_name
    while current and current not in seen:
        seen.add(current)
        entity_dir = entity_base / current
        tools_dir = entity_dir / 'tools'
        if tools_dir.is_dir() and any(tools_dir.glob('*.json')):
            return tools_dir
        # Follow extends
        yaml_path = entity_dir / 'agent.yaml'
        if not yaml_path.exists():
            break
        try:
            import yaml
            manifest = yaml.safe_load(yaml_path.read_text(encoding='utf-8')) or {}
            current = manifest.get('extends') or ''
        except Exception:
            break
    return None


def _entity_config_snapshot(entity_name: str, entity_base: Path) -> dict[str, str]:
    entity_dir = entity_base / entity_name
    if not entity_dir.exists():
        return {}
    snapshot: dict[str, str] = {}

    _PROMPT_MAP = [
        ('system_prompt', 'prompts/system.md', 'core/system.md'),
        ('heartbeat_prompt', 'prompts/heartbeat.md', 'core/heartbeat.md'),
        ('session_context_template', 'prompts/session.md', 'core/session.md'),
    ]
    try:
        from nutshell.session_engine.agent_loader import AgentLoader
        agent = AgentLoader().load(entity_dir)
        for attr, src_rel, dst_rel in _PROMPT_MAP:
            content = (getattr(agent, attr, None) or '').strip()
            if not content:
                # AgentLoader may return empty when prompts: section is absent from YAML;
                # fall back to reading the prompt file directly.
                src = entity_dir / src_rel
                content = src.read_text(encoding='utf-8').strip() if src.exists() else ''
            snapshot[dst_rel] = content
    except Exception:
        for _, src_rel, dst_rel in _PROMPT_MAP:
            src = entity_dir / src_rel
            snapshot[dst_rel] = src.read_text(encoding='utf-8').strip() if src.exists() else ''

    resolved_tools_dir = _resolve_entity_tools_dir(entity_name, entity_base)
    if resolved_tools_dir is not None:
        for src in sorted(resolved_tools_dir.glob('*.json')):
            raw = src.read_text(encoding='utf-8')
            try:
                normalized = json.dumps(json.loads(raw), sort_keys=True, ensure_ascii=False, indent=2)
            except Exception:
                normalized = raw.strip()
            snapshot[f'core/tools/{src.name}'] = normalized

    skills_dir = entity_dir / 'skills'
    if skills_dir.is_dir():
        for src in sorted(skills_dir.rglob('*.md')):
            rel = src.relative_to(skills_dir).as_posix()
            snapshot[f'core/skills/{rel}'] = src.read_text(encoding='utf-8')
    return snapshot


def _meta_config_snapshot(meta_dir: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for name in ('system.md', 'heartbeat.md', 'session.md'):
        path = meta_dir / 'core' / name
        snapshot[f'core/{name}'] = path.read_text(encoding='utf-8').strip() if path.exists() else ''

    tools_dir = meta_dir / 'core' / 'tools'
    if tools_dir.is_dir():
        for src in sorted(tools_dir.glob('*.json')):
            raw = src.read_text(encoding='utf-8')
            try:
                normalized = json.dumps(json.loads(raw), sort_keys=True, ensure_ascii=False, indent=2)
            except Exception:
                normalized = raw.strip()
            snapshot[f'core/tools/{src.name}'] = normalized

    skills_dir = meta_dir / 'core' / 'skills'
    if skills_dir.is_dir():
        for src in sorted(skills_dir.rglob('*.md')):
            rel = src.relative_to(skills_dir).as_posix()
            snapshot[f'core/skills/{rel}'] = src.read_text(encoding='utf-8')
    return snapshot


def _clear_dir_contents(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def populate_meta_from_entity(entity_name: str, entity_base: Path | None = None, s_base: Path | None = None) -> None:
    entity_root = entity_base or (_REPO_ROOT / 'entity')
    entity_dir = entity_root / entity_name
    if not entity_dir.exists():
        return

    meta_dir = ensure_meta_session(entity_name, s_base=s_base)
    core_dir = meta_dir / 'core'
    snapshot = _entity_config_snapshot(entity_name, entity_root)

    for rel in ('core/system.md', 'core/heartbeat.md', 'core/session.md'):
        (meta_dir / rel).write_text(snapshot.get(rel, ''), encoding='utf-8')

    tools_dir = core_dir / 'tools'
    skills_dir = core_dir / 'skills'
    _clear_dir_contents(tools_dir)
    _clear_dir_contents(skills_dir)

    src_tools = _resolve_entity_tools_dir(entity_name, entity_root)
    if src_tools is not None:
        for src in sorted(src_tools.glob('*.json')):
            shutil.copy2(src, tools_dir / src.name)

    src_skills = entity_dir / 'skills'
    if src_skills.is_dir():
        for src in sorted(src_skills.rglob('*')):
            rel = src.relative_to(src_skills)
            dst = skills_dir / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    params_src = entity_dir / 'agent.yaml'
    if params_src.exists():
        try:
            import yaml
            manifest = yaml.safe_load(params_src.read_text(encoding='utf-8')) or {}
            params = dict(manifest.get('params') or {})
            if manifest.get('model'):
                params['model'] = manifest['model']
            if manifest.get('provider'):
                params['provider'] = manifest['provider']
            (core_dir / 'params.json').write_text(json.dumps(params, indent=2, ensure_ascii=False), encoding='utf-8')
        except Exception:
            pass

    _mark_meta_synced(meta_dir, entity_name)


def compute_meta_diffs(entity_name: str, entity_base: Path | None = None, s_base: Path | None = None) -> list[dict]:
    entity_root = entity_base or (_REPO_ROOT / 'entity')
    entity_dir = entity_root / entity_name
    if not entity_dir.exists():
        return []
    meta_dir = get_meta_dir(entity_name, s_base=s_base)
    entity_snapshot = _entity_config_snapshot(entity_name, entity_root)
    meta_snapshot = _meta_config_snapshot(meta_dir)
    diffs: list[dict] = []
    for path in sorted(set(entity_snapshot) | set(meta_snapshot)):
        entity_val = entity_snapshot.get(path, '')
        meta_val = meta_snapshot.get(path, '')
        if entity_val != meta_val and entity_val:
            # Only flag when entity has content that differs — empty entity means
            # meta is free to use its own built-in defaults (e.g. meta-agent prompts).
            diffs.append({'path': path, 'entity': entity_val, 'meta': meta_val})
    return diffs


def check_meta_alignment(entity_name: str, entity_base: Path | None = None, s_base: Path | None = None) -> None:
    meta_dir = get_meta_dir(entity_name, s_base=s_base)
    if not _meta_is_synced(meta_dir):
        return
    diffs = compute_meta_diffs(entity_name, entity_base, s_base)
    if diffs:
        raise MetaAlignmentError(entity_name, diffs)


def sync_entity_to_meta(entity_name: str, entity_base: Path | None = None, s_base: Path | None = None) -> None:
    populate_meta_from_entity(entity_name, entity_base=entity_base, s_base=s_base)


def sync_meta_to_entity(entity_name: str, entity_base: Path | None = None, s_base: Path | None = None) -> None:
    entity_root = entity_base or (_REPO_ROOT / 'entity')
    entity_dir = entity_root / entity_name
    if not entity_dir.exists():
        return
    meta_dir = get_meta_dir(entity_name, s_base=s_base)
    core_dir = meta_dir / 'core'

    prompts_dir = entity_dir / 'prompts'
    prompts_dir.mkdir(parents=True, exist_ok=True)
    for src_name, dst_name in [('system.md', 'system.md'), ('heartbeat.md', 'heartbeat.md'), ('session.md', 'session.md')]:
        src = core_dir / src_name
        if src.exists():
            (prompts_dir / dst_name).write_text(src.read_text(encoding='utf-8'), encoding='utf-8')

    entity_tools = entity_dir / 'tools'
    entity_skills = entity_dir / 'skills'
    _clear_dir_contents(entity_tools)
    _clear_dir_contents(entity_skills)

    meta_tools = core_dir / 'tools'
    if meta_tools.is_dir():
        for src in sorted(meta_tools.glob('*.json')):
            shutil.copy2(src, entity_tools / src.name)

    meta_skills = core_dir / 'skills'
    if meta_skills.is_dir():
        for src in sorted(meta_skills.rglob('*')):
            rel = src.relative_to(meta_skills)
            dst = entity_skills / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    _mark_meta_synced(meta_dir, entity_name)


def sync_from_entity(entity_name: str, entity_base: Path | None = None, s_base: Path | None = None) -> None:
    """Bootstrap meta-session mutable state from the entity on first use.

    This seeds the meta session as the concrete entity instantiation unit for
    mutable state: primary memory, layered memory, and shared playground files.
    Existing meta-session files are preserved."""
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
    _sync_inherited_memory(entity_name, entity_root, core_dir)

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
    _sync_inherited_playground(entity_name, entity_root, meta_dir)


def _load_gene_commands(entity_name: str, entity_base: Path | None = None) -> list[str]:
    """Read the ``gene`` list from agent.yaml, walking the extends chain."""
    entity_root = entity_base or (_REPO_ROOT / 'entity')
    seen: set[str] = set()
    current = entity_name
    while current and current not in seen:
        seen.add(current)
        entity_dir = entity_root / current
        yaml_path = entity_dir / 'agent.yaml'
        if not yaml_path.exists():
            break
        try:
            import yaml
            manifest = yaml.safe_load(yaml_path.read_text(encoding='utf-8')) or {}
        except Exception:
            break
        gene = manifest.get('gene')
        if gene and isinstance(gene, list):
            return [str(cmd) for cmd in gene]
        current = manifest.get('extends') or ''
    return []


def run_gene_commands(
    entity_name: str,
    entity_base: Path | None = None,
    s_base: Path | None = None,
) -> None:
    """Execute the ``gene`` shell commands from agent.yaml in the meta playground.

    Each command runs with ``shell=True`` in the meta playground directory,
    with the meta-session venv activated via environment variables.
    Failures are printed but do not raise.
    After all commands finish, writes ``core/.gene_initialized`` marker.
    """
    commands = _load_gene_commands(entity_name, entity_base)
    if not commands:
        return

    meta_dir = ensure_meta_session(entity_name, s_base=s_base)
    playground_dir = meta_dir / 'playground'
    playground_dir.mkdir(parents=True, exist_ok=True)

    # Build env with venv activated
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

    # Write marker
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
# The meta session can run as a real agent session managed by the watcher.
# start_meta_agent() creates the _sessions/<entity>_meta/ system directory
# (manifest.json, status.json, etc.) so the watcher picks it up.

_META_SYSTEM_PROMPT = """You are the meta-agent for entity '{entity}'. Your role is to manage all agent sessions spawned by this entity.

Responsibilities:
- Periodically review all child sessions (the "dream" cycle)
- Extract key learnings and decisions into meta memory
- Track important ongoing work with explicit session references
- Clean up old, empty, or completed sessions
- Keep the entity's meta memory accurate and concise

You have access to bash to inspect sessions, read their content, and delete them when appropriate.
Meta-session memory is system-managed; keep it current through your own heartbeat-driven maintenance.
"""

_META_HEARTBEAT_PROMPT = """Dream cycle: Review all child sessions for this entity.

Steps:
1. List sessions: bash `nutshell sessions --json` (filter by entity from manifest.json in _sessions/)
2. For each session: check status, tasks.md content, last activity
3. Decide for each:
   - Still active or has pending tasks → keep, track in memory
   - Completed/stopped with valuable context → extract learnings to meta memory, then delete
   - Old, empty, or trivial → delete directly
4. Update meta memory with:
   - Tracked sessions list (session_id + purpose + path)
   - Key learnings extracted from archived sessions
5. Clean up: `rm -rf sessions/<id> _sessions/<id>` for sessions you're deleting
   Safety rule: NEVER delete sessions that are currently running or were active < 2 hours ago

Be intelligent — don't just follow rules mechanically. Consider context, task importance, and what's worth remembering.
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

    Creates _sessions/<entity>_meta/ with manifest.json, status.json, context.jsonl,
    events.jsonl. Writes built-in meta system.md and heartbeat.md to the meta
    session's core/ (only if those files are empty — entity prompts take precedence).
    Sets params for persistent agent with dream heartbeat.

    Idempotent — safe to call multiple times.
    Returns the system dir path (_sessions/<entity>_meta/).
    """
    from datetime import datetime
    from nutshell.session_engine.session_status import ensure_session_status
    from nutshell.session_engine.session_params import read_session_params, write_session_params

    sessions_base = s_base or _SESSIONS_DIR
    system_base = sys_base or (_REPO_ROOT / '_sessions')

    meta_id = get_meta_session_id(entity_name)
    meta_dir = ensure_meta_session(entity_name, s_base=sessions_base)
    system_dir = system_base / meta_id

    # ── 1. System directory (_sessions/<entity>_meta/) ──
    system_dir.mkdir(parents=True, exist_ok=True)

    # manifest.json (always overwrite to keep entity current)
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

    # context.jsonl + events.jsonl (idempotent)
    (system_dir / "context.jsonl").touch(exist_ok=True)
    (system_dir / "events.jsonl").touch(exist_ok=True)

    # status.json
    ensure_session_status(system_dir)

    # ── 2. Fallback prompts (only if empty) ──
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
            _META_HEARTBEAT_PROMPT.strip() + "\n",
            encoding="utf-8",
        )

    # ── 3. Params: merge meta-agent defaults without overwriting existing model/provider ──
    current_params = read_session_params(meta_dir)
    updates: dict = {}
    for key, default_val in _META_AGENT_DEFAULTS.items():
        if current_params.get(key) in (None, False, 0, ""):
            updates[key] = default_val
    if updates:
        write_session_params(meta_dir, **updates)

    return system_dir
