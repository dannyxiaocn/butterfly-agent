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
    EVENT_CONTROL_START,
    EVENT_CONTROL_STOP,
    EVENT_ERROR,
    EVENT_LLM_CALL_USAGE,
    EVENT_MODEL_STATUS,
    EVENT_PANEL_ENTRY_CHANGED,
    EVENT_PROMPT_CHANGED,
    EVENT_SESSION_CREATED,
    EVENT_SESSION_DELETED,
    EVENT_SUB_AGENT_COUNT,
    EVENT_SYSTEM_NOTICE,
    EVENT_TASK_CARD_CHANGED,
    EVENT_TASK_FINISHED,
    EVENT_TASK_SCRIPT_CHECK,
    EVENT_TASK_SCRIPT_ERROR,
    EVENT_TERMINAL_INPUT,
    EVENT_TERMINAL_LOG,
    EVENT_TERMINAL_STATE,
    EVENT_TODO_LIST_CHANGED,
    EVENT_TOOL_PROGRESS,
    EVENT_USER_INPUT,
    EVENT_USER_INTERRUPT,
    Event,
    SOURCE_CLI,
    SOURCE_SUBAGENT,
    SOURCE_TASK,
    SOURCE_WEB,
    append_event as _events_append_event,
)
from butterfly.runtime.events import (
    latest_event_id as _events_latest_event_id,
)
from butterfly.runtime.events import (
    read_events as _events_read_events,
)
from butterfly.runtime.events import (
    tail_events as _events_tail_events,
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

    # exclude_meta=False to mirror the pre-refactor web contract. CLI
    # aliases that want to hide meta sessions (e.g. ``butterfly sessions``)
    # filter at their own layer.
    live = _svc_list(_SESSIONS_DIR, _SYSTEM_SESSIONS_DIR, exclude_meta=False)
    if not include_archived or not _ARCHIVED_DIR.is_dir():
        return live
    # Archived sessions mirror the system-dir layout. We reuse the service
    # reader by pointing both base dirs at the archive path — it only
    # cares about manifest.json + status.json existence.
    archived = _svc_list(_ARCHIVED_DIR, _ARCHIVED_DIR, exclude_meta=False)
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


def tail_events(
    session_id: str,
    *,
    cursor: int | None = None,
    timeout: float = 30.0,
    poll_interval: float = 0.1,
):
    """Async-yield events with id > ``cursor`` as they land on disk.

    Thin session_id wrapper over the Phase 1 primitive
    ``butterfly.runtime.events.tail_events`` (DESIGN.md §5.2). Used by
    the web SSE endpoint to turn appended events into live pushes.

    Returns an ``AsyncIterator[Event]`` — see the primitive docstring
    for semantics (cursor=None yields everything from the start, timeout
    resets on each yield, poll_interval gates the file re-scan).
    """
    system_dir = _resolve_session_dir(session_id)
    return _events_tail_events(
        system_dir,
        cursor=cursor,
        timeout=timeout,
        poll_interval=poll_interval,
    )


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


# ─────────────────────────────────────────────────────────────────────────────
# WRITERS — Phase 5
# ─────────────────────────────────────────────────────────────────────────────
#
# Every function below is a mutating operation on a session's state.
# DESIGN.md §5.5 (API) + §5.6 (write semantics) + §5.7 (writer-derived
# state) spell out the contract:
#
#   1. Validate inputs. Raise ``ValueError`` / ``FileNotFoundError`` with
#      a clear message on bad input.
#   2. Resolve the session dir (via :func:`_resolve_session_dir`).
#   3. **Append the event to events_v1.jsonl FIRST** using the Phase 1
#      :func:`append_event` primitive. The event log is leading truth
#      (§5.6) — if a side-effect fails later, a future
#      ``rebuild_views(session_id)`` can reconcile.
#   4. Perform the side-effect (write task card, update config.yaml,
#      notify the live daemon, etc.).
#   5. Return the Event.
#
# Daemon-interacting writers (send_message / interrupt / stop / start /
# terminal_input) delegate the daemon-notify to existing service or
# bridge functions AFTER the event is written. Phase 11 removes those
# delegations once the daemon watches events_v1 natively. Each delegation
# is flagged with a TODO(phase11) comment.
#
# Invariant I5: the web UI (and CLI) writes through these functions only
# — no direct file IO elsewhere. Invariant I6: every function here is
# CLI-callable (Phase 6 wires ``butterfly io <name>`` reflection).


# ── Validators ────────────────────────────────────────────────────────────────

# Prompt / asset names — mirror butterfly.service.config_service so a
# writer behaves identically to its reader peer. Keeping the whitelist
# local avoids a reader-time import of config_service.
_ALLOWED_PROMPT_NAMES: frozenset[str] = frozenset({"system", "task", "env"})
_ALLOWED_ASSET_NAMES: frozenset[str] = frozenset({"tools", "skills"})


def _require_existing_session(session_id: str) -> Path:
    """Resolve + confirm the session exists. Mirrors reader behaviour but
    surfaces ``FileNotFoundError`` at the writer surface so the §5.6
    "validate first, then append" order is explicit.
    """
    return _resolve_session_dir(session_id)


# ── Session lifecycle ────────────────────────────────────────────────────────


def create_session(
    session_id: str,
    *,
    agent: str = "default",
    display_name: str | None = None,
    init_from: str | None = None,
) -> Event:
    """Create a fresh session and log the creation event.

    Order (§5.6):
      1. Validate inputs.
      2. Initialise the session directory structure (manifest, core/,
         events_v1.jsonl) via
         :func:`butterfly.service.sessions_service.create_session`, which
         materialises the canonical layout.
      3. Append ``EVENT_SESSION_CREATED`` to the new session's
         events_v1.jsonl. The event logically leads the session — it is
         always the id=1 entry.
      4. Return the Event.

    Rationale for the directory-first ordering: events_v1.jsonl lives
    INSIDE the session dir; we cannot append before the dir exists. Step
    2 is a controlled, idempotent setup that doesn't invoke the daemon
    or any derived write on peer sessions, so the "event-first" principle
    (§5.6) is preserved in spirit — it still precedes the side-effect
    that the event describes (i.e. notification to a consumer; the
    session dir is merely a prerequisite).

    ``init_from`` is reserved for the "fork an existing session" feature;
    Phase 5 does NOT implement it. Passing a non-None value raises
    ``NotImplementedError`` so callers don't silently get a fresh
    session when they asked for a clone.
    """
    _validate_session_id(session_id)
    if init_from is not None:
        raise NotImplementedError(
            "create_session: init_from=<id> (fork) is reserved for a future phase"
        )

    # Duplicate-id rejection before we touch any filesystem state.
    live = _SYSTEM_SESSIONS_DIR / session_id
    if live.is_dir():
        raise FileExistsError(f"session {session_id!r} already exists")

    from butterfly.service.sessions_service import create_session as _svc_create

    manifest = _svc_create(
        session_id,
        agent,
        _SESSIONS_DIR,
        _SYSTEM_SESSIONS_DIR,
        display_name=display_name,
    )
    system_dir = _SYSTEM_SESSIONS_DIR / session_id
    return _events_append_event(
        system_dir,
        EVENT_SESSION_CREATED,
        {"manifest": manifest},
    )


def delete_session(session_id: str) -> Event:
    """Log a deletion event then rmtree the session dirs.

    The event is appended BEFORE the directory is removed (§5.6) so a
    process reading events_v1 in the narrow window between event-write
    and rmtree observes the deletion signal. The event itself disappears
    with the directory — that's fine: the FUNCTIONAL signal is
    ``list_sessions()`` no longer returning the id.
    """
    system_dir = _require_existing_session(session_id)

    # Event first.
    event = _events_append_event(system_dir, EVENT_SESSION_DELETED, {})

    # Side-effect: remove both sibling dirs. Delegates to the service so
    # the status-file flip (write_session_status stopped) happens too.
    # TODO(phase11): inline the directory teardown here once the service
    # layer is retired.
    from butterfly.service.sessions_service import delete_session as _svc_delete

    _svc_delete(session_id, _SESSIONS_DIR, _SYSTEM_SESSIONS_DIR)
    return event


def start_session(session_id: str) -> Event:
    """Emit ``control_start`` + flip daemon status to active.

    Phase 5 dual-write: the event is the forward-looking signal (Phase
    11 has the daemon watching events_v1 natively) and the service call
    keeps today's status.json / pause-card behaviour intact.
    """
    system_dir = _require_existing_session(session_id)
    event = _events_append_event(system_dir, EVENT_CONTROL_START, {})

    # TODO(phase11): remove the service delegation once the daemon
    # reacts to EVENT_CONTROL_START directly.
    from butterfly.service.sessions_service import start_session as _svc_start

    _svc_start(session_id, _SYSTEM_SESSIONS_DIR)
    return event


def stop_session(session_id: str, *, reason: str = "user") -> Event:
    """Emit ``control_stop`` + flip daemon status to stopped."""
    system_dir = _require_existing_session(session_id)
    event = _events_append_event(
        system_dir, EVENT_CONTROL_STOP, {"reason": str(reason)}
    )

    # TODO(phase11): remove the service delegation once the daemon
    # reacts to EVENT_CONTROL_STOP directly.
    from butterfly.service.sessions_service import stop_session as _svc_stop

    _svc_stop(session_id, _SYSTEM_SESSIONS_DIR)
    return event


# ── Input ────────────────────────────────────────────────────────────────────


def send_message(
    session_id: str,
    text: str,
    *,
    source: str = "cli",
    caller: str | None = None,
    display_name: str | None = None,
) -> Event:
    """Append ``user_input`` then notify the live daemon (if any).

    ``source`` ∈ schema enum (cli / web / task / parent-agent). ``caller``
    carries the task card name (for the ``task`` source) or parent tool
    name (for the parent-agent source); ``None`` otherwise.

    Event is appended first (§5.6). Daemon notification (via
    :class:`BridgeSession`) is a best-effort post-event side-effect;
    failures don't suppress the event because the event itself is the
    canonical record — a daemon restart will pick up the pending input
    via ``Session._rebuild_history_from_events``.
    """
    if not isinstance(text, str):
        raise ValueError("send_message: text must be a string")
    _valid_sources = {SOURCE_CLI, SOURCE_WEB, SOURCE_TASK, SOURCE_SUBAGENT}
    if source not in _valid_sources:
        raise ValueError(
            f"send_message: unknown source {source!r}; "
            f"expected one of {sorted(_valid_sources)}"
        )
    system_dir = _require_existing_session(session_id)

    event = _events_append_event(
        system_dir,
        EVENT_USER_INPUT,
        {
            "text": text,
            "source": source,
            "caller": caller,
            "display_name": display_name,
        },
    )

    # TODO(phase11): daemon watches events_v1 natively — drop this call.
    # Today it writes a companion ``user_input`` entry to context.jsonl
    # and wakes the daemon via FileIPC; both are legacy channels.
    try:
        from butterfly.service.messages_service import send_message as _svc_send

        _svc_send(
            session_id,
            text,
            _SYSTEM_SESSIONS_DIR,
            caller=caller or "human",
            mode="interrupt",
        )
    except (FileNotFoundError, ValueError):
        # Daemon-notify is best-effort; event is already persisted.
        pass
    return event


def interrupt_session(session_id: str, *, text: str | None = None) -> Event:
    """Append ``user_interrupt`` then cancel the in-flight tick.

    One event covers both roles (§3.3):
      - ``text`` is ``None``: bare ⚡. ``llm_context`` skips the event
        when it builds messages, so no pollution of LLM context. The
        daemon sees the event and cancels.
      - ``text`` is a string: ⚡ with message. The event becomes a user
        turn in LLM context AND the daemon cancels first.

    We deliberately do NOT emit a separate ``EVENT_CONTROL_INTERRUPT``
    — the user-side event already carries the daemon signal through the
    event type, and emitting both would double-log the action.
    """
    if text is not None and not isinstance(text, str):
        raise ValueError("interrupt_session: text must be a string or None")
    system_dir = _require_existing_session(session_id)

    event = _events_append_event(
        system_dir, EVENT_USER_INTERRUPT, {"text": text}
    )

    # TODO(phase11): daemon watches events_v1 natively — drop this call.
    try:
        from butterfly.service.messages_service import (
            interrupt_session as _svc_interrupt,
        )

        _svc_interrupt(session_id, _SYSTEM_SESSIONS_DIR)
    except (FileNotFoundError, ValueError):
        pass

    # If the caller passed text, also deliver it as a user message so the
    # in-flight daemon has it in its inbox — matches the legacy ⚡+msg
    # behaviour. The event is already logged above; this is the
    # daemon-notify leg only.
    #
    # KNOWN ISSUE (Phase 11 fix): the service.send_message path triggers
    # the daemon's Phase 3a dual-emit, which will write a SECOND
    # EVENT_USER_INPUT event to events_v1 for the same text. In live
    # sessions this means `build_llm_context` sees the interrupt text
    # twice (once as user_interrupt, once as user_input). Phase 11
    # collapses this by having the daemon watch events_v1 natively
    # instead of going through service.send_message — the daemon will
    # then NOT re-emit on receipt, killing the duplication. Until then
    # the duplication is a visible but bounded cosmetic artifact on
    # live ⚡+message flows.
    if text:
        try:
            from butterfly.service.messages_service import (
                send_message as _svc_send,
            )

            _svc_send(
                session_id,
                text,
                _SYSTEM_SESSIONS_DIR,
                caller="human",
                mode="interrupt",
            )
        except (FileNotFoundError, ValueError):
            pass
    return event


# ── Tasks / todo ─────────────────────────────────────────────────────────────


def upsert_task(
    session_id: str,
    name: str,
    *,
    description: str | None = None,
    script: str | None = None,
    check_interval: float | None = None,
    notes: str | None = None,
    progress: str | None = None,
) -> Event:
    """Create or update a task card. Event-first.

    Order (§5.6):
      1. Validate name (reuse the ``_card_path`` validator via a dry-run
         call inside session_engine.task_cards).
      2. Resolve session, load any existing card, apply the patch in
         memory.
      3. Append ``EVENT_TASK_CARD_CHANGED`` with the PROJECTED card dict
         — this is the authoritative description of the change.
      4. Write the card file (``save_card``) + the script file
         (``write_script``) if a script body was supplied.
      5. Return the event.

    If the task doesn't exist AND no mutation parameters are given,
    raise ``ValueError("nothing to upsert")``. This guards against
    ``upsert_task(sid, "foo")`` silently creating a name-only card.

    ``notes`` maps to the card's ``comments`` field (the task_cards
    dataclass field is named ``comments``; we expose ``notes`` at the IO
    surface because the old tool spec normalised on "notes").
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("upsert_task: name must be a non-empty string")

    system_dir = _require_existing_session(session_id)
    user_dir = _resolve_user_dir(session_id)
    tasks_dir = user_dir / "core" / "tasks"

    from butterfly.session_engine.task_cards import (
        TaskCard,
        _card_path,  # name validator
        load_card,
        save_card,
        write_script,
    )

    # Validate the name by running it through ``_card_path`` — raises on
    # traversal / empty / "." name patterns.
    _card_path(tasks_dir, name)

    existing = load_card(tasks_dir, name)
    nothing_to_do = (
        existing is None
        and description is None
        and script is None
        and check_interval is None
        and notes is None
        and progress is None
    )
    if nothing_to_do:
        raise ValueError(
            "upsert_task: nothing to upsert — card does not exist and no "
            "fields supplied"
        )

    from datetime import datetime as _dt

    if existing is not None:
        card = TaskCard(
            name=name,
            description=description if description is not None else existing.description,
            status=existing.status,
            check_interval=(
                float(check_interval) if check_interval is not None
                else existing.check_interval
            ),
            created_at=existing.created_at,
            last_checked_at=existing.last_checked_at,
            last_started_at=existing.last_started_at,
            last_finished_at=existing.last_finished_at,
            comments=notes if notes is not None else existing.comments,
            progress=progress if progress is not None else existing.progress,
        )
    else:
        # Default cadence mirrors tasks_service: duty=7200s, others=3600s.
        default_interval = 7200.0 if name == "duty" else 3600.0
        card = TaskCard(
            name=name,
            description=description or "",
            status="pending",
            check_interval=(
                float(check_interval) if check_interval is not None
                else default_interval
            ),
            created_at=_dt.now().isoformat(),
            comments=notes or "",
            progress=progress or "",
        )

    # Event first (§5.6). Payload carries the full projected card so a
    # consumer can materialise state without re-reading the file.
    event = _events_append_event(
        system_dir,
        EVENT_TASK_CARD_CHANGED,
        {"name": name, "card": card.to_dict()},
    )

    # Side-effect: persist card + script.
    save_card(tasks_dir, card)
    if script is not None:
        write_script(tasks_dir, name, script)
    return event


def delete_task(session_id: str, name: str) -> Event:
    """Log the deletion then remove the card + script files.

    DESIGN.md §3.5 does not list a dedicated ``task_deleted`` event —
    ``task_card_changed`` carries both create/update and removal, with
    ``card=None`` signalling "gone". That keeps the event taxonomy
    smaller at the cost of one conditional on the reducer side.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("delete_task: name must be a non-empty string")
    system_dir = _require_existing_session(session_id)
    tasks_dir = _resolve_user_dir(session_id) / "core" / "tasks"

    from butterfly.session_engine.task_cards import (
        delete_card,
        load_card,
    )

    if load_card(tasks_dir, name) is None:
        raise FileNotFoundError(
            f"delete_task: card {name!r} does not exist in {session_id!r}"
        )

    event = _events_append_event(
        system_dir,
        EVENT_TASK_CARD_CHANGED,
        {"name": name, "card": None},
    )
    delete_card(tasks_dir, name)
    return event


def upsert_todo_list(session_id: str, todo_list: dict) -> Event:
    """Replace the session's todo list snapshot. Event-first."""
    if not isinstance(todo_list, dict):
        raise ValueError("upsert_todo_list: todo_list must be a dict")

    system_dir = _require_existing_session(session_id)
    core_dir = _resolve_user_dir(session_id) / "core"

    from butterfly.session_engine.todo_list import TodoList, save_todo_list

    try:
        todo = TodoList.from_dict(todo_list)
    except Exception as exc:  # defensive — from_dict is tolerant but guard anyway
        raise ValueError(f"upsert_todo_list: invalid todo_list payload: {exc}") from exc

    event = _events_append_event(
        system_dir,
        EVENT_TODO_LIST_CHANGED,
        {"todo_list": todo.to_dict()},
    )
    save_todo_list(core_dir, todo)
    return event


# ── Panel / terminal ─────────────────────────────────────────────────────────


def kill_panel_entry(session_id: str, tid: str) -> Event:
    """Mark a panel entry as killed. Event-first.

    The caller is responsible for any subprocess-side teardown (the web
    handler today calls ``BackgroundTaskManager.kill`` on the daemon
    side); this IO surface updates the file + fires the event so the
    UI reflects the kill immediately.
    """
    if not isinstance(tid, str) or not tid.strip():
        raise ValueError("kill_panel_entry: tid must be a non-empty string")
    system_dir = _require_existing_session(session_id)
    panel_dir = _resolve_user_dir(session_id) / "core" / "panel"

    from butterfly.session_engine.panel import (
        STATUS_KILLED,
        load_entry,
        save_entry,
    )

    entry = load_entry(panel_dir, tid)
    if entry is None:
        raise FileNotFoundError(
            f"kill_panel_entry: tid {tid!r} not found in {session_id!r}"
        )

    import time as _time

    entry.status = STATUS_KILLED
    entry.finished_at = _time.time()

    event = _events_append_event(
        system_dir,
        EVENT_PANEL_ENTRY_CHANGED,
        {"entry": entry.to_json()},
    )
    save_entry(panel_dir, entry)
    return event


def terminal_input(
    session_id: str,
    text: str,
    *,
    source: str = "web",
) -> Event:
    """Enqueue one line of terminal input. Event-first.

    Appends a ``terminal_input`` event (carries text + source for
    telemetry) then writes the entry to ``core/terminal/input.jsonl`` so
    the TerminalExecutor consumes it. The queue file is the effective
    channel today; Phase 11 can drop it once the executor watches
    events_v1 natively.
    """
    if not isinstance(text, str):
        raise ValueError("terminal_input: text must be a string")
    if source not in {"web", "cli"}:
        raise ValueError(f"terminal_input: unknown source {source!r}")
    system_dir = _require_existing_session(session_id)

    event = _events_append_event(
        system_dir,
        EVENT_TERMINAL_INPUT,
        {"text": text, "source": source},
    )

    # TODO(phase11): drop this — executor will watch events_v1.
    from butterfly.service.terminal_service import enqueue_input as _svc_enqueue

    _svc_enqueue(_SESSIONS_DIR, session_id, kind="input", content=text)
    return event


def terminal_interrupt(session_id: str) -> Event:
    """Interrupt the terminal's foreground process. Event-first.

    Encoded as a ``terminal_input`` event with ``text=None`` — the
    schema (§3.5) has one terminal-input event type; the None sentinel
    distinguishes interrupt from a regular line.
    """
    system_dir = _require_existing_session(session_id)

    event = _events_append_event(
        system_dir,
        EVENT_TERMINAL_INPUT,
        {"text": None, "source": "web"},
    )

    # TODO(phase11): drop this — executor will watch events_v1.
    from butterfly.service.terminal_service import enqueue_input as _svc_enqueue

    _svc_enqueue(_SESSIONS_DIR, session_id, kind="interrupt", content=None)
    return event


# ── Config / prompts / assets ────────────────────────────────────────────────


def update_config(session_id: str, key: str, value: Any) -> Event:
    """Update one key in ``core/config.yaml``. Event-first.

    Key is validated against the config whitelist
    (``DEFAULT_CONFIG`` keys) to prevent schema pollution — matches the
    service-layer guard in :func:`butterfly.service.config_service.update_config`.
    """
    from butterfly.session_engine.session_config import DEFAULT_CONFIG

    if not isinstance(key, str) or key not in DEFAULT_CONFIG:
        raise ValueError(
            f"update_config: unknown key {key!r}; "
            f"allowed={sorted(DEFAULT_CONFIG.keys())}"
        )
    system_dir = _require_existing_session(session_id)

    event = _events_append_event(
        system_dir, EVENT_CONFIG_CHANGED, {"key": key}
    )

    # Side-effect: single-key update via the service's params path so
    # duty-card sync + whitelist re-check still fire.
    # TODO(phase11): inline the yaml-merge once we retire the service.
    from butterfly.service.config_service import update_config as _svc_update

    _svc_update(
        session_id,
        _SESSIONS_DIR,
        _SYSTEM_SESSIONS_DIR,
        {key: value},
    )
    return event


def update_prompt(session_id: str, name: str, content: str) -> Event:
    """Overwrite ``core/<name>.md`` for prompts. Event-first.

    ``name`` ∈ {``system``, ``task``, ``env``}. Any other name raises
    ``ValueError`` (prevents directory traversal).
    """
    if name not in _ALLOWED_PROMPT_NAMES:
        raise ValueError(
            f"update_prompt: unknown prompt {name!r}; "
            f"expected one of {sorted(_ALLOWED_PROMPT_NAMES)}"
        )
    if not isinstance(content, str):
        raise ValueError("update_prompt: content must be a string")
    system_dir = _require_existing_session(session_id)

    event = _events_append_event(
        system_dir, EVENT_PROMPT_CHANGED, {"name": name}
    )

    # TODO(phase11): inline once service retires.
    from butterfly.service.config_service import update_prompt_md as _svc_prompt

    _svc_prompt(session_id, _SESSIONS_DIR, _SYSTEM_SESSIONS_DIR, name, content)
    return event


def update_asset(session_id: str, name: str, content: str) -> Event:
    """Overwrite ``core/<name>.md`` for assets. Event-first.

    ``name`` ∈ {``tools``, ``skills``}. Any other name raises
    ``ValueError``.
    """
    if name not in _ALLOWED_ASSET_NAMES:
        raise ValueError(
            f"update_asset: unknown asset {name!r}; "
            f"expected one of {sorted(_ALLOWED_ASSET_NAMES)}"
        )
    if not isinstance(content, str):
        raise ValueError("update_asset: content must be a string")
    system_dir = _require_existing_session(session_id)

    event = _events_append_event(
        system_dir, EVENT_ASSET_CHANGED, {"name": name}
    )

    # TODO(phase11): inline once service retires.
    from butterfly.service.config_service import update_asset_md as _svc_asset

    _svc_asset(session_id, _SESSIONS_DIR, _SYSTEM_SESSIONS_DIR, name, content)
    return event


# ── Public surface declaration ───────────────────────────────────────────────

__all__ = [
    # lifecycle
    "list_sessions",
    "get_session",
    "get_status",
    "create_session",
    "delete_session",
    "start_session",
    "stop_session",
    # events
    "read_events",
    "latest_event_id",
    "tail_events",
    # input
    "send_message",
    "interrupt_session",
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
    # writes
    "upsert_task",
    "delete_task",
    "upsert_todo_list",
    "kill_panel_entry",
    "terminal_input",
    "terminal_interrupt",
    "update_config",
    "update_prompt",
    "update_asset",
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
