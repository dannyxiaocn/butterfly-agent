from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from butterfly.session_engine.session_config import read_config
from .sessions_service import _validate_session_id


def get_hud(session_id: str, sessions_dir: Path, system_sessions_dir: Path) -> dict:
    _validate_session_id(session_id)
    system_dir = system_sessions_dir / session_id
    session_dir = sessions_dir / session_id
    if not system_dir.exists():
        raise FileNotFoundError(session_id)
    project_root = sessions_dir.parent
    git_root: str | None = None
    try:
        r = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=project_root, capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            git_root = r.stdout.strip()
    except Exception:
        pass
    git_added = git_deleted = git_files = 0
    if git_root:
        try:
            r = subprocess.run(['git', 'diff', '--shortstat', 'HEAD'], cwd=git_root, capture_output=True, text=True, timeout=3)
            if r.stdout:
                m = re.search(r'(\d+) files? changed', r.stdout)
                if m: git_files = int(m.group(1))
                m = re.search(r'(\d+) insertions?\(\+\)', r.stdout)
                if m: git_added = int(m.group(1))
                m = re.search(r'(\d+) deletions?\(-\)', r.stdout)
                if m: git_deleted = int(m.group(1))
        except Exception:
            pass
    params = read_config(session_dir) if session_dir.exists() else {}
    # Resolve the model label the HUD should display:
    #   1. explicit `model` field in config.yaml (user picked a specific one)
    #   2. otherwise look up the provider's default via the models catalog —
    #      the agent runs against that same default, so the HUD should show
    #      "gpt-5.4" rather than "(default)" which hides what's actually in use.
    display_model: str | None = params.get('model') or None
    if display_model is None:
        provider = params.get('provider') or None
        if provider:
            try:
                from .models_service import get_models_catalog
                catalog = get_models_catalog()
                for entry in catalog.get('providers', []):
                    if entry.get('provider') == provider:
                        default_model = entry.get('default_model')
                        if default_model:
                            display_model = f"{default_model} (provider default)"
                        break
            except Exception:
                pass
    from butterfly.runtime.ipc import FileIPC
    ipc = FileIPC(system_dir)
    latest_usage = None
    if ipc.context_path.exists():
        try:
            with open(ipc.context_path, 'rb') as f:
                lines = f.readlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    if ev.get('type') == 'turn' and ev.get('usage'):
                        latest_usage = ev['usage']
                        break
                except Exception:
                    continue
        except Exception:
            pass
    # Running sub_agent count, derived from on-disk panel entries so the
    # HUD can restore the badge after a page refresh — the SSE stream
    # only re-emits ``sub_agent_count`` when a child changes state.
    # (PR #28 review Gap #7.)
    sub_agents_running = 0
    if session_dir.exists():
        try:
            from butterfly.session_engine.panel import (
                list_entries as _list_entries,
                TYPE_SUB_AGENT as _TYPE_SUB_AGENT,
            )
            panel_dir = session_dir / 'core' / 'panel'
            sub_agents_running = sum(
                1 for e in _list_entries(panel_dir)
                if e.type == _TYPE_SUB_AGENT and not e.is_terminal()
            )
        except Exception:
            sub_agents_running = 0
    return {
        'cwd': git_root or str(project_root),
        'context_bytes': ipc.context_size(),
        'model': display_model,
        'git': {'files': git_files, 'added': git_added, 'deleted': git_deleted},
        'usage': latest_usage,
        'sub_agents_running': sub_agents_running,
    }
