"""Single IO surface for reading every session-visible piece of state.

Phase 4 of web-ui-refactor. See ``docs/refactor/DESIGN.md`` §5 (API) and
§5.4 (derived reads).

This module is the READER half. The writer half lands in Phase 5; until
then callers that need to write still go through the shim in
``butterfly/service/*``. Web (Phase 7) and CLI (Phase 6) will both swap
over to these readers.

Invariants I5/I6: the web UI never touches session files directly, and
every function here is CLI-callable. Session resolution is centralised
in ``_resolve_session_dir`` so the layout (``sessions/`` vs ``_sessions/``)
stays a single-knob concern.

Terminology. A "session_id" addresses two directories today:

    sessions/<id>/            user-visible content (core/, playground/, …)
    _sessions/<id>/           system state (manifest.json, status.json,
                              events_v1.jsonl)

The readers below take the session_id (string) and internally resolve
whichever directory they need. Callers never juggle two Paths.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Iterator

from butterfly.runtime.events import (
    EVENT_ASSET_CHANGED,
    EVENT_CONFIG_CHANGED,
    EVENT_ERROR,
    EVENT_LLM_CALL_USAGE,
    EVENT_MODEL_STATUS,
    EVENT_PANEL_ENTRY_CHANGED,
    EVENT_PROMPT_CHANGED,
    EVENT_SUB_AGENT_COUNT,
    EVENT_SYSTEM_NOTICE,
    EVENT_TASK_CARD_CHANGED,
    EVENT_TASK_FINISHED,
    EVENT_TASK_SCRIPT_CHECK,
    EVENT_TASK_SCRIPT_ERROR,
    EVENT_TERMINAL_LOG,
    EVENT_TERMINAL_STATE,
    EVENT_TODO_LIST_CHANGED,
    EVENT_TOOL_PROGRESS,
    Event,
)
from butterfly.runtime.events import (
    latest_event_id as _events_latest_event_id,
)
from butterfly.runtime.events import (
    read_events as _events_read_events,
)
from butterfly.runtime.llm_context import (
    Message,
    build_llm_context,
)


# ── Re-exported type aliases for the public surface ──────────────────────────
#
# We deliberately alias existing types rather than inventing new ones —
# DESIGN.md §5.4 lists these names, but each one maps to a concrete shape
# that already lives elsewhere. Centralising the aliases here means web
# and CLI can import ``from butterfly.runtime.io import ...`` without
# reaching into session_engine / service for types.

SessionInfo = dict  # shape matches ``butterfly.service.sessions_service.get_session``
SessionStatus = dict  # status.json payload, see session_status.DEFAULT_SESSION_STATUS
HudSnapshot = dict  # shape matches ``butterfly.service.hud_service.get_hud``
TodoList = dict  # shape matches ``butterfly.service.todo_list_service.get_todo_list``
TerminalState = dict
TerminalLogEntry = dict
PanelEntry = dict  # JSON form of ``butterfly.session_engine.panel.PanelEntry``


# ── Repo layout constants ─────────────────────────────────────────────────────
#
# Shared with ``butterfly.session_engine.agent_state`` — deliberately
# duplicated (not imported) so that agent_state's heavy imports don't
# pull into this module's startup cost. Both constants target the same
# filesystem locations.

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SESSIONS_DIR = _REPO_ROOT / "sessions"
_SYSTEM_SESSIONS_DIR = _REPO_ROOT / "_sessions"
_ARCHIVED_DIR = _REPO_ROOT / "_archived"  # future-proof; absent today

_SAFE_ID = re.compile(r"^[\w\-]+$")


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not _SAFE_ID.match(session_id):
        raise ValueError(f"Invalid session_id: {session_id!r}")


def _resolve_session_dir(session_id: str) -> Path:
    """Return the on-disk *system* directory for ``session_id``.

    Prefers live ``_sessions/<id>/`` then falls back to archived
    ``_archived/<id>/``. Raises FileNotFoundError if neither exists.

    The system dir is the authoritative directory — it holds
    ``manifest.json`` and ``events_v1.jsonl``. Callers that need the
    companion user dir (``sessions/<id>/core/…``) go through
    :func:`_resolve_user_dir`.
    """
    _validate_session_id(session_id)
    live = _SYSTEM_SESSIONS_DIR / session_id
    if live.is_dir():
        return live
    archived = _ARCHIVED_DIR / session_id
    if archived.is_dir():
        return archived
    raise FileNotFoundError(f"session {session_id!r} not found")


def _resolve_user_dir(session_id: str) -> Path:
    """Return the user-content directory (``sessions/<id>/``).

    The user dir is where task cards, config.yaml, prompts and panel
    entries live. It may not exist for archived sessions — callers treat
    a missing dir as "no content yet".
    """
    _validate_session_id(session_id)
    return _SESSIONS_DIR / session_id


# ── Session lifecycle ─────────────────────────────────────────────────────────


def list_sessions(include_archived: bool = False) -> list[SessionInfo]:
    """Enumerate sessions on disk.

    Delegates to ``butterfly.service.sessions_service.list_sessions`` for
    the live set so the sidebar sort order and ``SessionInfo`` fields stay
    identical to today's web API response. Archived sessions (when the
    ``_archived/`` dir exists) are merged in after the live set, sorted
    the same way. Today's repo ships no ``_archived/`` dir, so the flag
    is effectively future-proof.
    """
    from butterfly.service.sessions_service import list_sessions as _svc_list

    live = _svc_list(_SESSIONS_DIR, _SYSTEM_SESSIONS_DIR)
    if not include_archived or not _ARCHIVED_DIR.is_dir():
        return live
    # Archived sessions mirror the system-dir layout. We reuse the service
    # reader by pointing both base dirs at the archive path — it only
    # cares about manifest.json + status.json existence.
    archived = _svc_list(_ARCHIVED_DIR, _ARCHIVED_DIR)
    # Stamp an ``archived=True`` flag so callers can visually distinguish.
    for info in archived:
        info["archived"] = True
    return live + archived


def get_session(session_id: str) -> SessionInfo:
    """Return ``manifest.json`` + ``status.json`` + derived fields.

    Raises ``FileNotFoundError`` if ``session_id`` has no manifest in
    either the live or archived tree. Shape matches the existing
    ``/api/sessions/{id}`` response so Phase 7 can swap the handler body
    without a frontend contract change.
    """
    from butterfly.service.sessions_service import get_session as _svc_get

    _resolve_session_dir(session_id)  # validates + raises FileNotFoundError
    info = _svc_get(session_id, _SESSIONS_DIR, _SYSTEM_SESSIONS_DIR)
    if info is None:
        # Could happen for an archived-only session. Fall back to
        # reading the manifest from the archived tree manually.
        if (_ARCHIVED_DIR / session_id / "manifest.json").is_file():
            info = _svc_get(session_id, _ARCHIVED_DIR, _ARCHIVED_DIR)
            if info is not None:
                info["archived"] = True
                return info
        raise FileNotFoundError(f"session {session_id!r} not found")
    return info


def get_status(session_id: str) -> SessionStatus:
    """Return ``status.json`` for ``session_id`` (raw, defaults-merged).

    Thin wrapper over ``session_status.read_session_status`` — callers
    who only need ``pid`` / ``status`` / ``model_state`` shouldn't pay
    for the full ``get_session`` payload's manifest + tasks scan.
    """
    from butterfly.session_engine.session_status import read_session_status

    system_dir = _resolve_session_dir(session_id)
    return read_session_status(system_dir)


# ── Events primitives (re-exported for convenience) ──────────────────────────


def read_events(
    session_id: str,
    *,
    since_id: int | None = None,
    until_id: int | None = None,
    types: Iterable[str] | None = None,
) -> Iterator[Event]:
    """Yield events from ``events_v1.jsonl`` for ``session_id``.

    Thin session_id wrapper over the Phase 1 primitive
    ``butterfly.runtime.events.read_events``. Filter semantics are
    inherited verbatim (``since_id`` exclusive, ``until_id`` inclusive).
    Tolerates a missing events file (yields nothing) so callers can
    safely enumerate a freshly-created session.
    """
    system_dir = _resolve_session_dir(session_id)
    return _events_read_events(
        system_dir,
        since_id=since_id,
        until_id=until_id,
        types=types,
    )


def latest_event_id(session_id: str) -> int:
    """Return the largest event id in ``events_v1.jsonl`` (0 when empty)."""
    system_dir = _resolve_session_dir(session_id)
    return _events_latest_event_id(system_dir)


# ── Derived reads ─────────────────────────────────────────────────────────────


def read_llm_context(session_id: str) -> list[Message]:
    """Return provider-ready messages for ``session_id``.

    Exactly ``build_llm_context(read_events(session_id))``. Delegates to
    the Phase 2 pure builder so the contract (I3) is one function call.
    """
    return build_llm_context(read_events(session_id))


# Events the UI wants to render as cards even though they don't enter
# LLM context. Derived from DESIGN.md §3.5 — see the filter list in the
# original task spec. Any system event NOT in this set (e.g.
# ``control_interrupt``, ``session_started``, ``session_deleted``) is
# considered "plumbing" and MUST NOT appear in the frontend transcript.
_UI_VISIBLE_SYSTEM_TYPES: frozenset[str] = frozenset({
    EVENT_MODEL_STATUS,
    EVENT_LLM_CALL_USAGE,
    EVENT_TASK_CARD_CHANGED,
    EVENT_TASK_SCRIPT_CHECK,
    EVENT_TASK_SCRIPT_ERROR,
    EVENT_TASK_FINISHED,
    EVENT_TODO_LIST_CHANGED,
    EVENT_TOOL_PROGRESS,
    EVENT_PANEL_ENTRY_CHANGED,
    EVENT_SUB_AGENT_COUNT,
    EVENT_TERMINAL_LOG,
    EVENT_TERMINAL_STATE,
    EVENT_CONFIG_CHANGED,
    EVENT_PROMPT_CHANGED,
    EVENT_ASSET_CHANGED,
    EVENT_SYSTEM_NOTICE,
    EVENT_ERROR,
})


def read_display_history(session_id: str, *, since_id: int = 0) -> list[Event]:
    """Return UI-visible events in chronological order.

    Pulls straight from ``events_v1.jsonl`` without any position-pairing
    or turn-boundary re-derivation — in the events_v1 world, the events
    ARE the display data. Filter rule:

        - every ``for_llm=True`` event (user_input, user_interrupt,
          agent_text, agent_thinking, agent_tool_call,
          agent_tool_result)
        - system events whose type is in ``_UI_VISIBLE_SYSTEM_TYPES``

    Explicitly excluded: ``control_*`` and ``session_*`` lifecycle
    events (plumbing; never surfaced as cards).

    ``since_id`` is EXCLUSIVE — same semantics as
    :func:`butterfly.runtime.events.read_events`. Passing 0 (default)
    returns the full transcript.
    """
    out: list[Event] = []
    for ev in read_events(session_id, since_id=since_id):
        if ev.for_llm:
            out.append(ev)
        elif ev.type in _UI_VISIBLE_SYSTEM_TYPES:
            out.append(ev)
    return out


def read_hud(session_id: str) -> HudSnapshot:
    """Return the HUD snapshot (model, tokens, bg counts, git status, todo).

    Delegates to ``butterfly.service.hud_service.get_hud`` today — its
    logic already reads from ``events.jsonl`` for per-call context
    tokens / toks_per_s, from ``panel/`` for bg counts, and from
    ``config.yaml`` for the model name. A Phase 7 rewrite should migrate
    to events_v1 (``llm_call_usage`` + ``agent_tool_call`` pairing)
    instead of the legacy files.

    TODO(phase 7): replace delegation with an events_v1-native impl so
    the HUD rebuild survives the retirement of context.jsonl.
    """
    from butterfly.service.hud_service import get_hud as _svc_hud

    _resolve_session_dir(session_id)  # validates + raises
    return _svc_hud(session_id, _SESSIONS_DIR, _SYSTEM_SESSIONS_DIR)


def read_task_cards(session_id: str) -> list[dict]:
    """Return every task card under ``sessions/<id>/core/tasks/``.

    Each card is a dict with ``name``, ``description``, ``status``,
    ``check_interval``, plus the script body (``script`` field; None
    when absent). Cards are sorted with ``duty`` first (if present),
    then the rest alphabetically — matches what the web panel shows
    today.
    """
    from butterfly.session_engine.task_cards import (
        load_all_cards,
        read_script as _read_card_script,
    )

    _resolve_session_dir(session_id)  # validates + raises on unknown id
    tasks_dir = _resolve_user_dir(session_id) / "core" / "tasks"
    cards = sorted(
        load_all_cards(tasks_dir),
        key=lambda c: (c.name != "duty", c.name.lower()),
    )
    out: list[dict] = []
    for c in cards:
        d = c.to_dict()
        d["script"] = _read_card_script(tasks_dir, c.name)
        out.append(d)
    return out


def read_todo_list(session_id: str) -> TodoList | None:
    """Return the session's todo list snapshot, or ``None`` when empty.

    Shape matches ``butterfly.service.todo_list_service.get_todo_list``.
    ``None`` indicates either "no list has been created" or
    "list exists but has zero todos".
    """
    from butterfly.service.todo_list_service import get_todo_list as _svc_todo

    _resolve_session_dir(session_id)
    return _svc_todo(session_id, _SESSIONS_DIR)


def read_config(session_id: str) -> dict:
    """Return the session's ``core/config.yaml`` merged with defaults.

    Includes the ``is_meta_session`` flag (``True`` when the session id
    ends in ``_meta``) matching the existing ``/api/sessions/{id}/config``
    response shape. Raises ``FileNotFoundError`` when the session is
    missing in either the system or user tree.
    """
    from butterfly.service.config_service import get_config as _svc_config

    _resolve_session_dir(session_id)
    return _svc_config(session_id, _SESSIONS_DIR, _SYSTEM_SESSIONS_DIR)


def read_prompt(session_id: str, name: str) -> str:
    """Return the body of ``core/<name>.md`` (system / task / env).

    Returns empty string when the prompt file does not exist. Name is
    validated against the whitelist ``{system, task, env}`` — an
    unknown name raises ``ValueError`` to prevent directory traversal.
    """
    from butterfly.service.config_service import get_prompt_md as _svc_prompt

    _resolve_session_dir(session_id)
    return _svc_prompt(session_id, _SESSIONS_DIR, _SYSTEM_SESSIONS_DIR, name)


def read_asset(session_id: str, name: str) -> str:
    """Return the body of ``core/<name>.md`` for the tools/skills asset.

    Accepts ``tools`` or ``skills``. Empty string when the file does
    not exist. Any other name raises ``ValueError``.
    """
    from butterfly.service.config_service import get_asset_md as _svc_asset

    _resolve_session_dir(session_id)
    return _svc_asset(session_id, _SESSIONS_DIR, _SYSTEM_SESSIONS_DIR, name)


def read_panel(session_id: str) -> list[PanelEntry]:
    """Return every panel entry under ``sessions/<id>/core/panel/``.

    Each entry is the JSON serialisation of
    ``butterfly.session_engine.panel.PanelEntry``. Sorted ascending by
    ``created_at`` so the UI's chronological order is stable.
    Returns ``[]`` when the directory is missing.
    """
    from butterfly.session_engine.panel import list_entries

    _resolve_session_dir(session_id)
    panel_dir = _resolve_user_dir(session_id) / "core" / "panel"
    return [e.to_json() for e in list_entries(panel_dir)]


def read_panel_entry(session_id: str, tid: str) -> PanelEntry:
    """Return one panel entry by ``tid`` or raise ``FileNotFoundError``."""
    from butterfly.session_engine.panel import load_entry

    _resolve_session_dir(session_id)
    panel_dir = _resolve_user_dir(session_id) / "core" / "panel"
    entry = load_entry(panel_dir, tid)
    if entry is None:
        raise FileNotFoundError(f"panel entry {tid!r} not found in {session_id!r}")
    return entry.to_json()


def read_terminal_state(session_id: str) -> TerminalState:
    """Return ``core/terminal/state.json`` or the default-inactive shape."""
    from butterfly.service.terminal_service import read_state as _svc_state

    _resolve_session_dir(session_id)
    return _svc_state(_SESSIONS_DIR, session_id)


def read_terminal_log(
    session_id: str,
    *,
    since: int = 0,
) -> list[TerminalLogEntry]:
    """Return terminal log entries.

    Behaviour split on ``since``:

      - ``since == 0`` (default): return the last 500 entries in the log
        — a bounded initial panel fill.
      - ``since > 0``: read from that byte offset forward; useful for
        delta polls.

    Matches the two read modes the existing terminal service exposes
    (``read_log_tail`` + ``read_log_from``) but behind a single reader.
    """
    from butterfly.service.terminal_service import read_log_from, read_log_tail

    _resolve_session_dir(session_id)
    if since > 0:
        entries, _offset = read_log_from(_SESSIONS_DIR, session_id, since)
        return entries
    entries, _offset = read_log_tail(_SESSIONS_DIR, session_id)
    return entries


# ── Catalogs ──────────────────────────────────────────────────────────────────


def list_models() -> list[dict]:
    """Return the provider / model catalog from ``models.yaml``.

    Shape matches the existing ``/api/models`` response:
    ``[ {"provider": ..., "label": ..., "models": [...]}, ... ]`` — one
    entry per provider, ``models`` is a list of ``ModelSpec``-derived
    dicts. Phase 7 surfaces this to the frontend dropdown untouched.
    """
    from butterfly.service.models_service import get_models_catalog

    return get_models_catalog()["providers"]


def list_agents() -> list[str]:
    """Return agent names under ``agenthub/`` that ship a ``config.yaml``.

    Each entry is the directory name (no path), sorted alphabetically.
    Ignores hidden dirs. The frontend's "New session" dropdown reads
    straight from this list.
    """
    from butterfly.service.sessions_service import list_agents as _svc_agents

    agenthub_dir = _REPO_ROOT / "agenthub"
    return _svc_agents(agenthub_dir)


# ── Public surface declaration ───────────────────────────────────────────────

__all__ = [
    # lifecycle
    "list_sessions",
    "get_session",
    "get_status",
    # events
    "read_events",
    "latest_event_id",
    # derived
    "read_llm_context",
    "read_display_history",
    "read_hud",
    "read_task_cards",
    "read_todo_list",
    "read_config",
    "read_prompt",
    "read_asset",
    "read_panel",
    "read_panel_entry",
    "read_terminal_state",
    "read_terminal_log",
    # catalogs
    "list_models",
    "list_agents",
    # types
    "SessionInfo",
    "SessionStatus",
    "HudSnapshot",
    "TodoList",
    "TerminalState",
    "TerminalLogEntry",
    "PanelEntry",
]
