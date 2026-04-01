from __future__ import annotations

import difflib
import json
import shutil
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
    for fname in ('system.md', 'heartbeat.md', 'session.md', 'memory.md', 'tasks.md'):
        (core_dir / fname).touch(exist_ok=True)
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
        from nutshell.llm_engine.loader import AgentLoader
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

    tools_dir = entity_dir / 'tools'
    if tools_dir.is_dir():
        for src in sorted(tools_dir.glob('*.json')):
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

    src_tools = entity_dir / 'tools'
    if src_tools.is_dir():
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
        if entity_snapshot.get(path, '') != meta_snapshot.get(path, ''):
            diffs.append({'path': path, 'entity': entity_snapshot.get(path, ''), 'meta': meta_snapshot.get(path, '')})
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
