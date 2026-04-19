from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from butterfly.llm_engine.model_catalog import get_max_context_tokens
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
    from butterfly.runtime.ipc import FileIPC
    ipc = FileIPC(system_dir)
    # Turn-level cumulative usage (unchanged — still sourced from context.jsonl).
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
    # v2.0.19: per-LLM-call context + toks/s from events.jsonl. This is the
    # NEW authoritative source for HUD context-%; the prior ``context_bytes /
    # 4`` heuristic is kept only as a fallback for pre-v2.0.19 sessions that
    # never emitted ``llm_call_usage``.
    context_tokens = None
    toks_per_s = None
    if ipc.events_path.exists():
        try:
            with open(ipc.events_path, 'rb') as f:
                lines = f.readlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get('type') == 'llm_call_usage':
                    context_tokens = ev.get('context_tokens')
                    toks_per_s = ev.get('toks_per_s')
                    break
        except Exception:
            pass
    model_name = params.get('model') or None
    max_context_tokens = get_max_context_tokens(model_name)
    # Running bg work counts, derived from on-disk panel entries so the
    # HUD can restore the second-row badges after a page refresh / tab
    # switch — the SSE stream only re-emits ``sub_agent_count`` /
    # ``tool_finalize`` when a child changes state, so without these a
    # reload mid-run reads "0 running" for the entire lifetime of the
    # outstanding task. (PR #28 review Gap #7 for sub_agent; PR #43
    # review item #1 for bash.)
    sub_agents_running = 0
    bash_running = 0
    if session_dir.exists():
        try:
            from butterfly.session_engine.panel import (
                list_entries as _list_entries,
                TYPE_SUB_AGENT as _TYPE_SUB_AGENT,
                TYPE_PENDING_TOOL as _TYPE_PENDING_TOOL,
            )
            panel_dir = session_dir / 'core' / 'panel'
            for e in _list_entries(panel_dir):
                if e.is_terminal():
                    continue
                if e.type == _TYPE_SUB_AGENT:
                    sub_agents_running += 1
                elif e.type == _TYPE_PENDING_TOOL and e.tool_name == 'bash':
                    bash_running += 1
        except Exception:
            sub_agents_running = 0
            bash_running = 0
    # thinking_effort: whitelist against valid provider values so a typo
    # in config.yaml (e.g. ``hgih``) doesn't get painted next to the
    # model name as-is. Null when thinking is off or value is unknown.
    raw_effort = params.get('thinking_effort')
    thinking_effort = raw_effort if raw_effort in ('high', 'medium', 'low') else None
    return {
        'cwd': git_root or str(project_root),
        'context_bytes': ipc.context_size(),  # kept for legacy fallback in frontend
        'context_tokens': context_tokens,     # v2.0.19: last-call real token count
        'max_context_tokens': max_context_tokens,
        'toks_per_s': toks_per_s,
        'model': model_name,
        'thinking': bool(params.get('thinking')),
        'thinking_effort': thinking_effort,
        'git': {'files': git_files, 'added': git_added, 'deleted': git_deleted},
        'usage': latest_usage,
        'sub_agents_running': sub_agents_running,
        'bash_running': bash_running,
    }
