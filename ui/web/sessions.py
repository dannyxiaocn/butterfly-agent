"""Session helper functions: read metadata, sort, and initialize."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from nutshell.runtime.params import ensure_session_params, write_session_params
from nutshell.runtime.status import ensure_session_status, read_session_status, write_session_status


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, PermissionError, ValueError, OSError):
        return False


def _read_session_info(session_dir: Path, system_dir: Path) -> dict | None:
    """Read session metadata from manifest.json (static) and status.json (dynamic)."""
    manifest_path = system_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        manifest = {}
    status_payload = read_session_status(system_dir)
    tasks_path = session_dir / "core" / "tasks.md"
    has_tasks = tasks_path.exists() and bool(tasks_path.read_text(encoding="utf-8").strip())
    tasks_mtime = (
        datetime.fromtimestamp(tasks_path.stat().st_mtime).isoformat()
        if tasks_path.exists() else None
    )
    pid_alive = _pid_alive(status_payload.get("pid"))
    status = status_payload.get("status", "active")
    return {
        "id": system_dir.name,
        "entity": manifest.get("entity", "?"),
        "created_at": manifest.get("created_at", ""),
        "heartbeat": manifest.get("heartbeat", 10.0),
        "pid_alive": pid_alive,
        "status": status,
        "has_tasks": has_tasks,
        "model_state": status_payload.get("model_state", "idle"),
        "model_source": status_payload.get("model_source"),
        "last_run_at": status_payload.get("last_run_at"),
        "tasks_updated_at": tasks_mtime,
        "heartbeat_interval": status_payload.get("heartbeat_interval", 600.0),
        "alive": pid_alive and status != "stopped",
    }


def _session_priority(info: dict) -> int:
    """Return sort priority: 0=running, 1=napping(tasks queued), 2=stopped, 3=idle."""
    if info.get("model_state") == "running" and info.get("pid_alive") and info.get("status") != "stopped":
        return 0
    if info.get("has_tasks") and info.get("pid_alive") and info.get("status") != "stopped":
        return 1
    if info.get("status") == "stopped":
        return 2
    return 3


def _sort_sessions(sessions: list[dict]) -> list[dict]:
    """Sort sessions: running > queued > idle > stopped, then by most recently run."""
    sessions.sort(key=lambda s: s.get("last_run_at") or s.get("created_at") or "", reverse=True)
    sessions.sort(key=_session_priority)
    return sessions


def _write_if_absent(path: Path, content: str) -> None:
    """Write content to path only if it does not already exist."""
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _init_session(
    sessions_dir: Path,
    system_sessions_dir: Path,
    session_id: str,
    entity: str,
    heartbeat: float,
) -> None:
    """Initialize a new session directory structure by copying entity content to core/.

    Idempotent: only writes files that do not already exist, except manifest.json.
    """
    session_dir = sessions_dir / session_id
    system_dir = system_sessions_dir / session_id
    core_dir = session_dir / "core"

    core_dir.mkdir(parents=True, exist_ok=True)
    (core_dir / "tools").mkdir(exist_ok=True)
    (core_dir / "skills").mkdir(exist_ok=True)
    (session_dir / "docs").mkdir(exist_ok=True)
    (session_dir / "playground").mkdir(exist_ok=True)
    system_dir.mkdir(parents=True, exist_ok=True)

    (system_dir / "context.jsonl").touch(exist_ok=True)
    (system_dir / "events.jsonl").touch(exist_ok=True)

    manifest = {
        "session_id": session_id,
        "entity": entity,
        "created_at": datetime.now().isoformat(),
    }
    (system_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    entity_path = Path(entity)
    agent = None
    if entity_path.exists():
        try:
            from nutshell import AgentLoader
            agent = AgentLoader().load(entity_path)
        except Exception as e:
            print(f"[web] Warning: failed to load entity '{entity}': {e}")

    if agent is not None:
        _write_if_absent(core_dir / "system.md", agent.system_prompt or "")
        _write_if_absent(core_dir / "heartbeat.md", agent.heartbeat_prompt or "")
        _write_if_absent(core_dir / "session_context.md", agent.session_context_template or "")

        for t in agent.tools:
            tool_json = core_dir / "tools" / f"{t.name}.json"
            if not tool_json.exists():
                schema = {"name": t.name, "description": t.description, "input_schema": t.schema}
                tool_json.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")

        for s in agent.skills:
            skill_dir = core_dir / "skills" / s.name
            if not skill_dir.exists():
                if s.location is not None:
                    src_dir = s.location.parent
                    if src_dir.is_dir():
                        shutil.copytree(src_dir, skill_dir, dirs_exist_ok=True)
                    else:
                        skill_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(s.location, skill_dir / "SKILL.md")
                else:
                    skill_dir.mkdir(parents=True, exist_ok=True)
                    content = f"---\nname: {s.name}\ndescription: {s.description}\n---\n\n{s.body}\n"
                    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

        if not (core_dir / "params.json").exists():
            from nutshell.runtime.provider_factory import provider_name as pname
            entity_provider = pname(agent._provider) or "anthropic"
            write_session_params(session_dir, heartbeat_interval=heartbeat,
                                 model=agent.model, provider=entity_provider)
        else:
            write_session_params(session_dir, heartbeat_interval=heartbeat)
    else:
        for fname in ("system.md", "heartbeat.md", "session_context.md"):
            _write_if_absent(core_dir / fname, "")
        if not (core_dir / "params.json").exists():
            ensure_session_params(session_dir, heartbeat_interval=heartbeat)
        else:
            write_session_params(session_dir, heartbeat_interval=heartbeat)

    _write_if_absent(core_dir / "memory.md", "")
    _write_if_absent(core_dir / "tasks.md", "")

    ensure_session_status(system_dir)
    write_session_status(system_dir, heartbeat_interval=heartbeat)
