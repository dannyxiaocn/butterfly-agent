from __future__ import annotations

from pathlib import Path

from butterfly.session_engine.session_config import (
    DEFAULT_CONFIG,
    read_config,
    write_config,
)
from .sessions_service import _validate_session_id, is_meta_session

# Whitelist of keys update_config_yaml / update_config will persist. Anything
# else is silently dropped to prevent schema pollution through the network-
# reachable PUT endpoints. DEFAULT_CONFIG is the sole source of truth.
_ALLOWED_KEYS = frozenset(DEFAULT_CONFIG.keys())


def get_config(session_id: str, sessions_dir: Path, system_sessions_dir: Path) -> dict:
    _validate_session_id(session_id)
    session_dir = sessions_dir / session_id
    system_dir = system_sessions_dir / session_id
    if not system_dir.exists() or not session_dir.exists():
        raise FileNotFoundError(session_id)
    cfg = read_config(session_dir)
    return {**cfg, 'is_meta_session': is_meta_session(session_id)}


def update_config(session_id: str, sessions_dir: Path, system_sessions_dir: Path, params: dict) -> dict:
    _validate_session_id(session_id)
    from butterfly.session_engine.task_cards import ensure_card, load_card, save_card
    session_dir = sessions_dir / session_id
    system_dir = system_sessions_dir / session_id
    if not system_dir.exists() or not session_dir.exists():
        raise FileNotFoundError(session_id)
    # Whitelist the incoming keys against DEFAULT_CONFIG. The PUT endpoints are
    # network-reachable; without this, a client could persist arbitrary keys
    # into the YAML file which then round-trip forever via read_config()'s
    # "{**DEFAULT_CONFIG, **raw}" merge. Not a security issue per-se, but it
    # silently corrupts the schema.
    params = {k: v for k, v in dict(params).items() if k in _ALLOWED_KEYS}

    # Sync duty config field with task card
    duty = params.get('duty')
    if isinstance(duty, dict) and duty.get('interval'):
        tasks_dir = session_dir / 'core' / 'tasks'
        existing = load_card(tasks_dir, 'duty')
        if existing is not None:
            existing.interval = float(duty['interval'])
            existing.description = duty.get('description', existing.description)
            save_card(tasks_dir, existing)
        else:
            ensure_card(tasks_dir, name='duty', interval=float(duty['interval']), description=duty.get('description', ''))

    write_config(session_dir, **params)
    saved = read_config(session_dir)
    return {**saved, 'is_meta_session': is_meta_session(session_id)}


# ── Per-file editors for tools.md / skills.md / prompts/*.md ──────────────────

_ALLOWED_ASSET_NAMES = frozenset({"tools", "skills"})
_ALLOWED_PROMPT_NAMES = frozenset({"system", "task", "env"})


def _asset_path(session_id: str, sessions_dir: Path, system_sessions_dir: Path, name: str) -> Path:
    if name not in _ALLOWED_ASSET_NAMES:
        raise ValueError(f"unknown asset: {name}")
    _validate_session_id(session_id)
    session_dir = sessions_dir / session_id
    system_dir = system_sessions_dir / session_id
    if not system_dir.exists() or not session_dir.exists():
        raise FileNotFoundError(session_id)
    return session_dir / "core" / f"{name}.md"


def _prompt_path(session_id: str, sessions_dir: Path, system_sessions_dir: Path, name: str) -> Path:
    if name not in _ALLOWED_PROMPT_NAMES:
        raise ValueError(f"unknown prompt: {name}")
    _validate_session_id(session_id)
    session_dir = sessions_dir / session_id
    system_dir = system_sessions_dir / session_id
    if not system_dir.exists() or not session_dir.exists():
        raise FileNotFoundError(session_id)
    # Sessions store prompts flat under core/<name>.md (Agent-level agenthub/
    # dirs use prompts/<name>.md; copy performed by session_init).
    return session_dir / "core" / f"{name}.md"


def get_asset_md(session_id: str, sessions_dir: Path, system_sessions_dir: Path, name: str) -> str:
    path = _asset_path(session_id, sessions_dir, system_sessions_dir, name)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def update_asset_md(session_id: str, sessions_dir: Path, system_sessions_dir: Path, name: str, text: str) -> str:
    path = _asset_path(session_id, sessions_dir, system_sessions_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path.read_text(encoding="utf-8")


def get_prompt_md(session_id: str, sessions_dir: Path, system_sessions_dir: Path, name: str) -> str:
    path = _prompt_path(session_id, sessions_dir, system_sessions_dir, name)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def update_prompt_md(session_id: str, sessions_dir: Path, system_sessions_dir: Path, name: str, text: str) -> str:
    path = _prompt_path(session_id, sessions_dir, system_sessions_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path.read_text(encoding="utf-8")
