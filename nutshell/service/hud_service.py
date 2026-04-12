from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from nutshell.session_engine.session_config import read_config
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
    from nutshell.runtime.ipc import FileIPC
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
    return {
        'cwd': git_root or str(project_root),
        'context_bytes': ipc.context_size(),
        'model': params.get('model') or None,
        'git': {'files': git_files, 'added': git_added, 'deleted': git_deleted},
        'usage': latest_usage,
    }
