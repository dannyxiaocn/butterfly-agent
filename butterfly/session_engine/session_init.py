"""Shared session initialization — creates session directory structure from an agent.

Used by:
  - butterfly/service/sessions_service.py  (web UI new-session endpoint)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from butterfly.session_engine.session_config import read_config, write_config, ensure_config
from butterfly.session_engine.session_status import ensure_session_status, write_session_status
from butterfly.session_engine.task_cards import ensure_card
from butterfly.session_engine.agent_state import (
    ensure_gene_initialized,
    ensure_meta_session,
    get_meta_version,
    populate_meta_from_agent,
    start_meta_agent,
    sync_from_agent,
)

_REPO_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_SESSIONS_BASE = _REPO_ROOT / "sessions"
_DEFAULT_SYSTEM_SESSIONS_BASE = _REPO_ROOT / "_sessions"
_DEFAULT_AGENT_BASE = _REPO_ROOT / "agenthub"
_TOOLHUB_DIR = _REPO_ROOT / "toolhub"
_VALID_MODES = frozenset({"explorer", "executor"})


def _write_if_absent(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _is_real_memory(content: str) -> bool:
    """Return True if content has meaningful memory, not just a seed placeholder."""
    stripped = content.strip()
    if not stripped:
        return False
    # Filter out placeholder-only content: lines that are headings or "(empty..." markers
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    return any(not ln.startswith("#") and not ln.startswith("(empty") for ln in lines)


def _create_session_venv(session_dir: Path) -> Path:
    """Create a Python venv at session_dir/.venv (idempotent).

    Uses --system-site-packages so all globally installed packages are
    available without re-installing.  Returns the venv path.

    Race-safe: if two processes attempt concurrent creation (same session_id
    generated within the same second), the loser catches CalledProcessError
    and returns the venv that the winner already created.
    """
    venv_path = session_dir / ".venv"
    if venv_path.exists():
        return venv_path
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(venv_path)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        # Another process may have won the race and already created a valid venv.
        # Check pyvenv.cfg (created last by venv) as the completion sentinel.
        if (venv_path / "pyvenv.cfg").exists():
            return venv_path
        raise
    return venv_path


_DISPLAY_NAME_MAX_LEN = 40


def _normalize_display_name(raw: str | None) -> str | None:
    """Trim + length-cap a display name. Returns None when empty/absent.

    Accepts ``None`` (no display name) or a non-empty trimmed string up to
    ``_DISPLAY_NAME_MAX_LEN`` chars. Longer names are truncated rather than
    rejected so the caller never hits a surprise 400. Non-string inputs
    are treated as absent — the `sub_agent` tool boundary enforces string
    type strictly via `_validate_name`, so coercing here would only hide
    callers that pass the wrong shape (PR #37 review).
    """
    if not isinstance(raw, str):
        return None
    trimmed = raw.strip()
    if not trimmed:
        return None
    return trimmed[:_DISPLAY_NAME_MAX_LEN]


def init_session(
    session_id: str,
    agent_name: str,
    *,
    sessions_base: Path | None = None,
    system_sessions_base: Path | None = None,
    agent_base: Path | None = None,
    initial_message: str | None = None,
    initial_message_id: str | None = None,
    parent_session_id: str | None = None,
    mode: str | None = None,
    sub_agent_depth: int | None = None,
    display_name: str | None = None,
) -> str:
    """Create a new session on disk from an agent, ready for the server to pick up.

    Returns the session_id. Idempotent: only writes files that do not exist yet.

    Args:
        session_id:          The unique session identifier (e.g. '2026-03-25_10-00-00').
        agent_name:          Name of the agent in agent_base/ (e.g. 'agent', 'butterfly_dev').
        sessions_base:       Root of agent-visible sessions/ directory.
        system_sessions_base: Root of _sessions/ directory.
        agent_base:          Root of agenthub/ directory.
        initial_message:     Optional first user message to write to context.jsonl.
        initial_message_id:  Optional UUID for the initial message — lets the
                              caller correlate the eventual reply (sub_agent
                              uses this to call BridgeSession.async_wait_for_reply).
        parent_session_id:   Optional parent session — recorded in manifest so
                              sidebar/services can render the session hierarchy.
        mode:                Optional sub-agent mode: "explorer" | "executor".
                              When set, ``toolhub/sub_agent/<mode>.md`` is
                              copied to the child's ``core/mode.md`` and the
                              mode name is recorded in the manifest. The mode
                              prompt is concatenated into ``system_prompt`` by
                              ``Session._load_session_capabilities`` (which
                              sits in the static prefix later rendered by
                              ``Agent._build_system_parts``).
                              Raises ``FileNotFoundError`` if the matching
                              ``toolhub/sub_agent/<mode>.md`` asset is absent —
                              recording a mode in the manifest without its
                              prompt on disk would leave the child in an
                              inconsistent state (raised in PR #28 review).
    """
    if mode is not None and mode not in _VALID_MODES:
        raise ValueError(f"init_session: invalid mode {mode!r}; expected one of {sorted(_VALID_MODES)}")
    s_base = sessions_base or _DEFAULT_SESSIONS_BASE
    sys_base = system_sessions_base or _DEFAULT_SYSTEM_SESSIONS_BASE
    ent_base = agent_base or _DEFAULT_AGENT_BASE

    session_dir = s_base / session_id
    system_dir = sys_base / session_id
    core_dir = session_dir / "core"

    # Create directory tree
    core_dir.mkdir(parents=True, exist_ok=True)
    (core_dir / "tools").mkdir(exist_ok=True)
    (core_dir / "skills").mkdir(exist_ok=True)
    (session_dir / "docs").mkdir(exist_ok=True)
    (session_dir / "playground").mkdir(exist_ok=True)
    system_dir.mkdir(parents=True, exist_ok=True)

    context_path = system_dir / "context.jsonl"
    events_path = system_dir / "events.jsonl"
    context_path.touch(exist_ok=True)
    events_path.touch(exist_ok=True)

    # Create session-level Python venv (idempotent)
    _create_session_venv(session_dir)

    # NOTE (first-run race fix — see docs/butterfly/session_engine/design.md):
    # manifest.json is the watcher's discovery signal. It MUST be written LAST —
    # after sessions/<id>/core/config.yaml is populated with the agent's real
    # model and provider. If we publish manifest.json first, the server-side
    # watcher races us: it spawns Session(session_id) whose Session.__init__
    # calls ensure_config(session_dir), which writes DEFAULT_CONFIG
    # (model=None, provider=None) before init_session's config copy runs.
    # Our `if not session_config_path.exists()` guard then skips the real
    # copy, leaving model=null persisted on disk. The manifest write is
    # deferred to the end of this function for that reason.
    agent_dir = ent_base / agent_name

    # Config always comes from meta session; meta is initially populated from agent.
    meta_dir = ensure_meta_session(agent_name, s_base=s_base)
    if agent_dir.exists():
        meta_config = meta_dir / 'core' / 'config.yaml'
        if not meta_config.exists() or not meta_config.read_text(encoding='utf-8').strip():
            populate_meta_from_agent(agent_name, ent_base, s_base)
        ensure_gene_initialized(agent_name, ent_base, s_base)
        start_meta_agent(agent_name, agent_base=ent_base, s_base=s_base, sys_base=sys_base)

    meta_core_dir = meta_dir / "core"
    # Copy prompts
    for name in ("system.md", "task.md", "env.md"):
        src = meta_core_dir / name
        _write_if_absent(core_dir / name, src.read_text(encoding="utf-8") if src.exists() else "")

    # Copy tools.md from meta or agent (toolhub-based tool list), fallback to legacy tool.md
    for tools_md_src in (meta_core_dir / "tools.md", agent_dir / "tools.md",
                         meta_core_dir / "tool.md", agent_dir / "tool.md"):
        if tools_md_src.exists():
            _write_if_absent(core_dir / "tools.md", tools_md_src.read_text(encoding="utf-8"))
            break

    # Copy skills.md from meta or agent (skillhub-based skill list)
    for skills_md_src in (meta_core_dir / "skills.md", agent_dir / "skills.md"):
        if skills_md_src.exists():
            _write_if_absent(core_dir / "skills.md", skills_md_src.read_text(encoding="utf-8"))
            break

    # Copy config.yaml from meta (or agent) into session core/.
    #
    # If a stub config.yaml already exists (e.g. a racing Session.__init__
    # called ensure_config() and wrote DEFAULT_CONFIG before we got here),
    # we still need to seed model/provider from the agent — otherwise the
    # session ships with `model: null` and the agent has no model to run
    # (v2.0.8 first-run bug).
    meta_config_path = meta_core_dir / "config.yaml"
    session_config_path = core_dir / "config.yaml"

    def _needs_seed(path: Path) -> bool:
        if not path.exists():
            return True
        try:
            import yaml as _yaml
            loaded = _yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            return True
        # safe_load returns None for empty files, and may return lists/scalars
        # for malformed-but-valid YAML. Anything that isn't a mapping is
        # treated as "needs seed" — safer to overwrite a broken stub with the
        # real agent config than to trust it.
        if not isinstance(loaded, dict):
            return True
        # Stub DEFAULT_CONFIG written by ensure_config has model=None and
        # provider=None; real agent configs always carry both. If either
        # is missing/falsy, re-seed from the agent.
        return not loaded.get("model") or not loaded.get("provider")

    if _needs_seed(session_config_path):
        if meta_config_path.exists() and meta_config_path.read_text(encoding="utf-8").strip():
            shutil.copy2(meta_config_path, session_config_path)
        else:
            # No meta config yet — bootstrap from agent config.yaml
            agent_config_path = agent_dir / "config.yaml"
            if agent_config_path.exists():
                shutil.copy2(agent_config_path, session_config_path)
            else:
                ensure_config(session_dir)
    # Record meta version in status.json so staleness can be detected later.
    meta_version = get_meta_version(agent_name, sys_base=sys_base)
    if meta_version:
        write_session_status(system_dir, agent_version=meta_version)
    # Seed mutable state from meta session, with agent memory as bootstrap fallback.
    sync_from_agent(agent_name, ent_base, s_base=s_base)

    meta_memory = meta_dir / "core" / "memory.md"
    agent_memory = (ent_base / agent_name / "memory.md") if agent_dir.exists() else None
    if meta_memory.exists() and _is_real_memory(meta_memory.read_text(encoding="utf-8")):
        _write_if_absent(core_dir / "memory.md", meta_memory.read_text(encoding="utf-8"))
    elif agent_memory and agent_memory.exists() and _is_real_memory(agent_memory.read_text(encoding="utf-8")):
        _write_if_absent(core_dir / "memory.md", agent_memory.read_text(encoding="utf-8"))
    else:
        _write_if_absent(core_dir / "memory.md", "")

    # Seed layered memory from <agent>_meta/core/memory/ first, then agenthub/<agent>/memory/.
    memory_seed_dirs = [src_dir for src_dir in (meta_dir / "core" / "memory", ent_base / agent_name / "memory") if src_dir.is_dir()]
    seed_files = [f for src_dir in memory_seed_dirs for f in sorted(src_dir.glob("*.md"))]
    if seed_files:
        session_memory_dir = core_dir / "memory"
        session_memory_dir.mkdir(exist_ok=True)
        for src_file in seed_files:
            dst_file = session_memory_dir / src_file.name
            if not dst_file.exists() and _is_real_memory(src_file.read_text(encoding="utf-8")):
                shutil.copy2(src_file, dst_file)

    # Seed shared playground files from meta-session without overwriting session-local files.
    meta_playground_dir = meta_dir / "playground"
    if meta_playground_dir.is_dir():
        session_playground_dir = session_dir / "playground"
        for src_path in sorted(meta_playground_dir.rglob("*")):
            if src_path.is_dir():
                continue
            rel = src_path.relative_to(meta_playground_dir)
            dst_path = session_playground_dir / rel
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            if not dst_path.exists():
                shutil.copy2(src_path, dst_path)

    # Create task cards directory; seed duty card if config defines one
    tasks_dir = core_dir / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    # Create panel directory (non-blocking tool state + future sub-agent refs)
    (core_dir / "panel").mkdir(parents=True, exist_ok=True)
    session_cfg = read_config(session_dir)
    duty = session_cfg.get("duty")
    if isinstance(duty, dict) and duty.get("interval"):
        ensure_card(
            tasks_dir,
            name="duty",
            interval=float(duty["interval"]),
            description=duty.get("description", ""),
        )

    ensure_session_status(system_dir)

    # Parent playground hand-off (sub-agent only). When a child is spawned
    # by sub_agent, we surface the parent's playground inside the child's
    # own playground as a symlink — ``playground/parent/`` → parent's
    # playground. The child can READ those files freely (Guardian only
    # blocks writes); explorer-mode writes resolve through the symlink to
    # the parent's tree, which is OUTSIDE the child's Guardian root, so
    # they are denied. Reuses the existing playground bound — no extra
    # contract needed. (PR #28 review Gap #6.)
    if parent_session_id is not None:
        parent_playground = s_base / parent_session_id / "playground"
        if parent_playground.is_dir():
            link_target = session_dir / "playground" / "parent"
            if not link_target.exists() and not link_target.is_symlink():
                try:
                    link_target.symlink_to(parent_playground.resolve(), target_is_directory=True)
                except OSError:
                    # Symlink creation may fail on exotic filesystems; the
                    # child will still work, just without the convenience link.
                    pass

    # Mode prompt — copy toolhub/sub_agent/<mode>.md into core/mode.md.
    # Session._load_session_capabilities folds it into the static
    # (cacheable) system prefix consumed by Agent._build_system_parts.
    #
    # We hard-fail when the prompt file is missing: recording ``mode`` in
    # the manifest without its corresponding prompt would activate the
    # Guardian boundary (in explorer mode) and the sidebar chip without
    # the agent-visible rules that make those mechanisms safe. Cubic
    # review (PR #28) flagged the silent-skip path as a consistency hole.
    if mode is not None:
        mode_src = _TOOLHUB_DIR / "sub_agent" / f"{mode}.md"
        if not mode_src.exists():
            raise FileNotFoundError(
                f"init_session: mode={mode!r} requires {mode_src} to exist; "
                "the child would otherwise end up in an inconsistent state "
                "(manifest says mode=X but no prompt was injected)."
            )
        _write_if_absent(core_dir / "mode.md", mode_src.read_text(encoding="utf-8"))

    # Publish manifest LAST (see NOTE above about watcher race):
    # by the time manifest.json is visible to the watcher, sessions/<id>/core/
    # has a fully-populated config.yaml, so Session.__init__'s ensure_config
    # is a no-op instead of clobbering model/provider with DEFAULT_CONFIG.
    manifest: dict = {
        "session_id": session_id,
        "agent": agent_name,
        "created_at": datetime.now().isoformat(),
    }
    if parent_session_id is not None:
        manifest["parent_session_id"] = parent_session_id
    if mode is not None:
        manifest["mode"] = mode
    if sub_agent_depth is not None:
        manifest["sub_agent_depth"] = int(sub_agent_depth)
    normalized_display = _normalize_display_name(display_name)
    if normalized_display:
        manifest["display_name"] = normalized_display
    (system_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Write optional initial message
    if initial_message:
        import uuid
        event = {
            "type": "user_input",
            "content": initial_message,
            "id": initial_message_id or str(uuid.uuid4()),
            "ts": datetime.now().isoformat(),
        }
        with context_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    return session_id
