from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from butterfly.session_engine.session_config import read_config
from butterfly.session_engine.session_status import read_session_status, write_session_status, pid_alive as _pid_alive


_SAFE_ID = re.compile(r'^[\w\-]+$')


def _validate_session_id(session_id: str) -> None:
    if not _SAFE_ID.match(session_id):
        raise ValueError(f"Invalid session_id: {session_id!r}")


def list_agents(agenthub_dir: Path) -> list[str]:
    """Return names of agents in agenthub/ that ship a config.yaml."""
    if not agenthub_dir.is_dir():
        return []
    return sorted(
        d.name for d in agenthub_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".") and (d / "config.yaml").is_file()
    )


def _is_stale_stopped(info: dict) -> bool:
    if info.get("status") != "stopped":
        return False
    ts = info.get("stopped_at") or info.get("updated_at")
    if not ts:
        return False
    try:
        stopped_at = datetime.fromisoformat(ts)
    except Exception:
        return False
    now = datetime.now(stopped_at.tzinfo) if stopped_at.tzinfo is not None else datetime.now()
    return (now - stopped_at).total_seconds() >= 12 * 3600


def get_session(session_id: str, sessions_dir: Path, system_sessions_dir: Path) -> dict | None:
    _validate_session_id(session_id)
    session_dir = sessions_dir / session_id
    system_dir = system_sessions_dir / session_id
    manifest_path = system_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        manifest = {}
    status_payload = read_session_status(system_dir)
    params = read_config(session_dir) if session_dir.exists() else {}
    from butterfly.session_engine.task_cards import has_pending_cards
    tasks_dir = session_dir / "core" / "tasks"
    has_tasks = has_pending_cards(tasks_dir)
    cards_mtimes = [f.stat().st_mtime for f in list(tasks_dir.glob("*.json")) + list(tasks_dir.glob("*.md"))] if tasks_dir.is_dir() else []
    tasks_mtime = datetime.fromtimestamp(max(cards_mtimes)).isoformat() if cards_mtimes else None
    pid_alive = _pid_alive(status_payload.get("pid"))
    status = status_payload.get("status", "active")
    return {
        "id": session_id,
        "agent": manifest.get("agent", "?"),
        "created_at": manifest.get("created_at", ""),
        "pid_alive": pid_alive,
        "status": status,
        "has_tasks": has_tasks,
        "model_state": status_payload.get("model_state", "idle"),
        "model_source": status_payload.get("model_source"),
        "last_run_at": status_payload.get("last_run_at"),
        "updated_at": status_payload.get("updated_at"),
        "stopped_at": status_payload.get("stopped_at"),
        "tasks_updated_at": tasks_mtime,
        "params": params,
        "alive": pid_alive and status != "stopped",
        # Sub-agent fields (populated by init_session when applicable; absent
        # for top-level sessions). Surfaced so the sidebar can render parent →
        # child indentation and the panel can show the mode tag.
        "parent_session_id": manifest.get("parent_session_id"),
        "mode": manifest.get("mode"),
        # User-facing name (optional; set by sub_agent tool's ``name`` arg or
        # by the web new-session form). Falls back to session_id in the UI
        # when absent.
        "display_name": manifest.get("display_name"),
    }


def _last_activity_key(info: dict) -> str:
    """Timestamp used for sidebar sorting.

    Prefer the last time the session actually ran a turn; fall back to the
    mtime of the most recent task card update, then to creation time. This
    keeps a freshly-woken idle session above a stale one that was last
    running a week ago — sort is strictly by recency regardless of state.
    """
    return (
        info.get("last_run_at")
        or info.get("updated_at")
        or info.get("tasks_updated_at")
        or info.get("created_at")
        or ""
    )


def sort_sessions(sessions: list[dict]) -> list[dict]:
    sessions.sort(key=_last_activity_key, reverse=True)
    return sessions


def list_sessions(sessions_dir: Path, system_sessions_dir: Path) -> list[dict]:
    if not system_sessions_dir.is_dir():
        return []
    result = []
    for d in sorted(system_sessions_dir.iterdir()):
        if not d.is_dir():
            continue
        info = get_session(d.name, sessions_dir, system_sessions_dir)
        if info is not None:
            result.append(info)
    return sort_sessions(result)


def create_session(
    session_id: str,
    agent: str,
    sessions_dir: Path,
    system_sessions_dir: Path,
    *,
    display_name: str | None = None,
) -> dict:
    """Create a new session.

    ``display_name`` is the user-facing label shown in the sidebar and panel;
    the internal ``session_id`` (timestamp + 4-char uuid suffix) stays the
    canonical, unique identifier. Pass ``None`` to create an unnamed session
    (UI will fall back to the session_id).

    The returned ``display_name`` is the **normalized** value (trimmed +
    capped at 40 chars) — i.e. exactly what was persisted to the manifest.
    This keeps the `POST /api/sessions` response consistent with every
    subsequent `GET /api/sessions` read (PR #37 review finding #1).
    """
    _validate_session_id(session_id)
    from butterfly.session_engine.session_init import (
        init_session, init_team_session, _normalize_display_name,
    )
    from butterfly.session_engine.agent_config import AgentConfig
    from butterfly.session_engine.team import is_team_manifest

    agent_path = Path(agent)
    if len(agent_path.parts) >= 2 and agent_path.parts[0] == "agenthub":
        agent_name = str(Path(*agent_path.parts[1:]))
        agent_base = sessions_dir.parent / "agenthub"
    elif agent_path.is_absolute() or agent_path.parent != Path('.'):
        agent_name = agent_path.name
        agent_base = agent_path.parent.resolve() if not agent_path.is_absolute() else agent_path.parent
    else:
        agent_name = agent
        agent_base = sessions_dir.parent / "agenthub"
    normalized_name = _normalize_display_name(display_name)

    # Branch on the agenthub config's ``kind`` field — team configs spawn
    # a team session (router daemon + N member sub-sessions) instead of a
    # single Agent session. ``is_team_manifest`` reads the manifest dict
    # directly so we don't have to import the full TeamSpec just to peek.
    is_team = False
    try:
        cfg = AgentConfig.from_path(agent_base / agent_name)
        is_team = is_team_manifest(cfg.manifest)
    except (FileNotFoundError, OSError, ValueError):
        # Single-agent path is the legacy fallback — failing to read a
        # config.yaml is not fatal at this layer (init_session itself
        # raises a clearer error downstream).
        is_team = False

    if is_team:
        init_team_session(
            team_session_id=session_id,
            team_name=agent_name,
            sessions_base=sessions_dir,
            system_sessions_base=system_sessions_dir,
            agent_base=agent_base,
        )
    else:
        init_session(
            session_id=session_id,
            agent_name=agent_name,
            sessions_base=sessions_dir,
            system_sessions_base=system_sessions_dir,
            agent_base=agent_base,
            display_name=normalized_name,
        )
    return {
        "id": session_id,
        "agent": agent,
        "display_name": normalized_name,
        "kind": "team" if is_team else "agent",
    }


def delete_session(session_id: str, sessions_dir: Path, system_sessions_dir: Path) -> bool:
    _validate_session_id(session_id)
    system_dir = system_sessions_dir / session_id
    session_dir = sessions_dir / session_id
    if not system_dir.exists() and not session_dir.exists():
        return False
    write_session_status(system_dir, status="stopped", pid=None, stopped_at=datetime.now().isoformat())
    if session_dir.exists():
        shutil.rmtree(session_dir)
    if system_dir.exists():
        shutil.rmtree(system_dir)
    return True


def _emit_task_changes_bulk(
    session_id: str,
    changed_names: list[str],
    change: str,
    system_sessions_dir: Path,
) -> None:
    """Emit one ``task_card_changed`` event per affected card name so the
    web UI's on-event Tasks refresh fires for each visible row (v2.0.30).

    Kept service-local (not in tasks_service) because only Start/Stop
    mutate multiple cards atomically; the single-card path already
    has its own emitter.
    """
    if not changed_names:
        return
    system_dir = system_sessions_dir / session_id
    if not system_dir.exists():
        return
    from butterfly.runtime.ipc import FileIPC
    ipc = FileIPC(system_dir)
    for name in changed_names:
        try:
            ipc.append_event({
                "type": "task_card_changed",
                "card": name,
                "change": change,
            })
        except OSError:
            pass


def stop_session(session_id: str, system_sessions_dir: Path) -> bool:
    _validate_session_id(session_id)
    system_dir = system_sessions_dir / session_id
    if not (system_dir / 'manifest.json').exists():
        return False
    write_session_status(system_dir, status="stopped", pid=None, stopped_at=datetime.now().isoformat())
    # v2.0.24: also pause every active task card so the session is fully
    # quiet while stopped (otherwise pending cards would re-fire the moment
    # the user resumes). Resolved via the session's tasks_dir to keep this
    # service-layer call free of per-call disk-layout knowledge.
    #
    # v2.0.30: pause_all_cards now returns the affected names; emit a
    # per-card task_card_changed event so the Tasks tab refreshes
    # on-event when the user clicks Stop.
    sessions_base = _resolve_sessions_base(system_sessions_dir)
    affected: list[str] = []
    if sessions_base is not None:
        try:
            from butterfly.session_engine.task_cards import pause_all_cards
            affected = pause_all_cards(sessions_base / session_id / "core" / "tasks")
        except Exception:
            affected = []  # best-effort — stop should still succeed
    _emit_task_changes_bulk(session_id, affected, "paused", system_sessions_dir)
    # v2.0.24: dropped the "paused — use ▶ Start to resume" status row.
    # The sidebar already renders the stopped state via /api/sessions; the
    # context-stream notice was redundant chrome.
    return True


def start_session(session_id: str, system_sessions_dir: Path) -> bool:
    _validate_session_id(session_id)
    system_dir = system_sessions_dir / session_id
    if not (system_dir / 'manifest.json').exists():
        return False
    write_session_status(system_dir, status="active", stopped_at=None)
    # v2.0.24: symmetric to stop_session — un-pause every card we paused on
    # Stop. Cards manually paused by the user via CLI/UI also flip back; if
    # someone wants per-card persistence across Start they can re-pause
    # after resume. Best-effort.
    #
    # v2.0.30: resume_all_paused_cards now returns the affected names;
    # emit per-card events so the Tasks tab refreshes on Start.
    sessions_base = _resolve_sessions_base(system_sessions_dir)
    affected: list[str] = []
    if sessions_base is not None:
        try:
            from butterfly.session_engine.task_cards import resume_all_paused_cards
            affected = resume_all_paused_cards(sessions_base / session_id / "core" / "tasks")
        except Exception:
            affected = []
    _emit_task_changes_bulk(session_id, affected, "resumed", system_sessions_dir)
    # v2.0.24: dropped the "resumed" context-stream notice — see stop_session.
    return True


def _resolve_sessions_base(system_sessions_dir: Path) -> Path | None:
    """Map ``_sessions/`` → ``sessions/`` so we can reach a session's task
    cards from a service that only knows the system dir.

    The pair always lives side-by-side under the same parent; relying
    on the trailing-segment swap keeps the service layer free of a
    global config import and naturally handles test tmp_path layouts
    that mirror the same structure. Returns None when the parent layout
    doesn't match — caller treats that as "best-effort skip".
    """
    parent = system_sessions_dir.parent
    sibling_name = system_sessions_dir.name.lstrip("_") or "sessions"
    candidate = parent / sibling_name
    return candidate if candidate.is_dir() else None
