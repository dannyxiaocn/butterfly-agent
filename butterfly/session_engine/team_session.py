"""TeamSession — runs the team-level "router daemon" for a `kind: team` session.

The team session has *no* LLM and *no* Agent. Its only jobs:

  1. Tail ``_sessions/<team_id>/context.jsonl`` for ``user_input`` events
     (the user typing into the team chat in the UI).

  2. Forward each ``user_input`` to the right member: the @-mentioned
     member when the text contains ``@<name>``, otherwise the configured
     ``leader``. Forwarding goes through ``BridgeSession.send_message``
     against the member's child session, which is just a regular Session
     daemon driven by the watcher.

  3. Keep teamchat coherent: when forwarding, also prepend any unread
     teamchat summary to the message body so the recipient sees what
     happened while they were idle, and bump the recipient's cursor to
     the latest seq the moment we deliver. This is the *natural-wakeup*
     summary path (silent-mode unread); the @-driven interrupt path is
     handled by the ``teamchat_send`` tool itself.

The router runs the same ``run_daemon_loop(ipc, stop_event=...)`` shape as
``Session`` so the watcher can ``await`` it identically.

This module contains zero LLM / Agent imports — the team session is pure
plumbing — keeping cold-start latency low for sessions that are merely
displaying a static team feed.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from butterfly.runtime.bridge import BridgeSession
from butterfly.session_engine.session_status import (
    ensure_session_status, write_session_status,
)
from butterfly.session_engine.teamchat import (
    TeamChat, cursor_get, cursor_set, parse_mentions,
)

if TYPE_CHECKING:
    from butterfly.runtime.ipc import FileIPC


_log = logging.getLogger(__name__)


class TeamSession:
    """Drive the router loop for a `kind: team` session.

    Constructed from the team session id + base paths; the watcher
    instantiates one of these instead of ``Session`` whenever the session's
    manifest has ``kind == "team"``.
    """

    _POLL_INTERVAL = 0.05

    def __init__(
        self,
        team_session_id: str,
        *,
        base_dir: Path,
        system_base: Path,
    ) -> None:
        self._team_id = team_session_id
        self._base_dir = Path(base_dir)
        self._system_base = Path(system_base)
        self.session_dir = self._base_dir / team_session_id
        self.system_dir = self._system_base / team_session_id
        self.core_dir = self.session_dir / "core"
        self._manifest = self._read_team_manifest()
        self._members_map: dict[str, str] = dict(
            self._manifest.get("members_map") or {}
        )
        # member_name → mode (default | silent). Materialise once at startup;
        # re-read on a config edit only if we add a hot-reload path later.
        self._member_modes: dict[str, str] = {
            row.get("name", ""): row.get("teamchat_mode", "default")
            for row in (self._manifest.get("members") or [])
            if isinstance(row, dict) and row.get("name")
        }
        self._leader: str = self._manifest.get("leader") or ""
        self._teamchat = TeamChat(
            self.core_dir, list(self._members_map.keys())
        )

    # ── manifest ──────────────────────────────────────────────────────────

    def _read_team_manifest(self) -> dict:
        path = self.system_dir / "manifest.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    # ── routing ───────────────────────────────────────────────────────────

    def _pick_recipient(self, text: str) -> str:
        """Return the member name to deliver this user input to.

        The first @-mention that names a real member wins. Falls back to
        the team's leader. ``@all`` is ignored for routing — the user
        addressing everyone still has to land on one queue, and broadcasting
        from the user side would create N concurrent agent runs from a
        single human turn (out of scope for v1).

        Mention parsing is delegated to ``teamchat.parse_mentions`` so the
        router and the teamchat tool agree on the rules: case-insensitive
        match against the roster, a `(?<![A-Za-z0-9_])` lookbehind that
        keeps email addresses (``user@coder.com``) from being misread as
        a real mention, and unknown handles dropped silently.
        """
        member_names = list(self._members_map.keys())
        for name in parse_mentions(text or "", member_names):
            if name == "all":
                continue
            if name in self._members_map:
                return name
        return self._leader

    def _member_system_dir(self, member_name: str) -> Path | None:
        sid = self._members_map.get(member_name)
        if not sid:
            return None
        return self._system_base / sid

    def _build_wakeup_payload(self, member_name: str, original_text: str) -> str:
        """Compose the text we deliver to a recipient on user input.

        Wraps the user message with any teamchat unread summary the
        recipient hasn't seen yet, then advances the recipient's cursor
        so subsequent natural wakeups don't re-show the same summary.
        """
        member_sys_dir = self._member_system_dir(member_name)
        if member_sys_dir is None:
            return original_text
        cursor = cursor_get(member_sys_dir)
        unread = self._teamchat.messages_since(cursor)
        if unread:
            summary = self._teamchat.summary_lines(
                unread, viewer=member_name
            )
            cursor_set(member_sys_dir, unread[-1].seq)
            if summary:
                return f"{summary}\n\n---\n\n{original_text}"
        return original_text

    async def _route_user_input(self, content: str) -> None:
        """Forward one user_input row to the matching member."""
        recipient = self._pick_recipient(content)
        if not recipient:
            _log.warning(
                "team %s: no recipient for input; leader=%r members=%s",
                self._team_id, self._leader, list(self._members_map),
            )
            return
        member_sys_dir = self._member_system_dir(recipient)
        if member_sys_dir is None:
            _log.warning(
                "team %s: recipient %r has no member session dir",
                self._team_id, recipient,
            )
            return
        payload = self._build_wakeup_payload(recipient, content)
        try:
            BridgeSession(member_sys_dir).send_message(
                payload,
                caller="team_router",
                mode="interrupt",
            )
        except Exception as exc:  # noqa: BLE001 — best-effort fan-out
            _log.warning(
                "team %s: failed to forward to %s: %s",
                self._team_id, recipient, exc,
            )

    def _initial_input_offset(self) -> int:
        """Byte offset in the team's ``context.jsonl`` to start polling from.

        Mirrors the rule that single-agent ``Session._initial_input_offset``
        encodes: rewind to 0 on fresh sessions so any seed ``user_input``
        written by ``init_team_session(initial_message=...)`` is picked up,
        and otherwise resume at end-of-file so prior already-routed rows
        aren't replayed on watcher restart.

        Team sessions have no ``turn`` events to use as a watermark
        (they don't run an Agent), so the fresh-vs-resume distinction is
        just "is the file empty?". The router is idempotent on duplicate
        input — re-routing a row only re-fans it to the same recipient
        — but doing it on every restart would still echo the original
        input on every daemon respawn, hence the EOF resume rule.
        """
        ctx_path = self.system_dir / "context.jsonl"
        if not ctx_path.exists():
            return 0
        try:
            size = ctx_path.stat().st_size
        except OSError:
            return 0
        # Fresh: only seed rows present (or genuinely empty file). Treat
        # both as "rewind to 0" — there are no committed turns to skip.
        # The presence of any row implies we should consider this a
        # post-seed state, but on the first daemon start we still want to
        # read the seed. Distinguish: if a status event has already been
        # written to events.jsonl (i.e. a previous daemon was up), resume
        # at EOF; else rewind to 0.
        events_path = self.system_dir / "events.jsonl"
        had_prior_run = (
            events_path.exists()
            and events_path.stat().st_size > 0
        )
        if had_prior_run:
            return size
        return 0

    # ── daemon loop ───────────────────────────────────────────────────────

    async def run_daemon_loop(
        self,
        ipc: "FileIPC",
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Tail context.jsonl for user_input rows and route them.

        Same signature as ``Session.run_daemon_loop`` so the watcher can
        treat both kinds of sessions uniformly.
        """
        # Compute the input offset BEFORE we write the
        # ``team_router_started`` status event — otherwise events.jsonl
        # has data on fresh sessions and the resume-at-EOF branch fires
        # incorrectly, dropping the ``initial_message`` seed.
        input_offset = self._initial_input_offset()
        ensure_session_status(self.system_dir)
        write_session_status(
            self.system_dir, model_state="idle", model_source="team_router"
        )
        ipc.append_event({
            "type": "status", "value": "team_router_started",
        })
        try:
            while True:
                inputs, input_offset = ipc.poll_inputs(input_offset)
                for msg in inputs:
                    # Only forward genuine user-typed inputs. Synthetic
                    # turn rows produced by ``teamchat_send`` (sender =
                    # member name) are visible to the UI but must not be
                    # re-routed — that would echo every member message
                    # back into the team router as a fresh user_input.
                    caller = msg.get("caller") or "human"
                    if caller != "human":
                        continue
                    content = msg.get("content") or ""
                    if not content.strip():
                        continue
                    await self._route_user_input(content)
                if stop_event is not None and stop_event.is_set():
                    break
                await asyncio.sleep(self._POLL_INTERVAL)
        except asyncio.CancelledError:
            ipc.append_event({
                "type": "status", "value": "team_router_cancelled",
            })
            raise
        ipc.append_event({
            "type": "status", "value": "team_router_stopped",
        })


# ── helpers exported for the watcher / sessions service ──────────────────────


def is_team_session(system_dir: Path) -> bool:
    """Return True when ``_sessions/<id>/manifest.json`` declares kind=team."""
    path = system_dir / "manifest.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (data.get("kind") or "agent") == "team"
