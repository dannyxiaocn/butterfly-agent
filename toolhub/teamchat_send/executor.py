"""Executor for ``teamchat_send`` — post a teamchat message + fan out to peers.

Lives in toolhub/ (not under butterfly/) so the loader can pick it up via the
generic ``Executor`` lookup. Construction happens in
``butterfly/tool_engine/loader.py``: when a member session has manifest field
``member_of_team``, the loader builds a ``TeamchatSendExecutor`` with all the
context it needs (team core_dir, this member's system_dir + name, the member
list).

Behaviour per recipient (other than the sender):

  * ``default`` mode → ``send_message(mode=interrupt)`` with the message body
    wrapped together with the recipient's unread teamchat summary; cursor
    advances to the latest seq the moment we deliver.

  * ``silent`` mode + recipient is in ``mentions`` (or ``all``) →
    same as default (the @-mention is the explicit interrupt trigger).

  * ``silent`` mode + recipient not mentioned → no live delivery; the
    message simply stays in teamchat.jsonl and surfaces on the
    recipient's next natural wakeup via the unread summary built by the
    team router.

The team session also gets a synthetic ``user_input`` row with the sender's
name as ``caller`` so the team-chat UI renders the post inline. We
deliberately do NOT use ``turn`` rows here — turns carry too many invariant
fields (usage, thinking_blocks, …) that we'd be faking.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from butterfly.runtime.bridge import BridgeSession
from butterfly.session_engine.teamchat import (
    TeamChat, cursor_get, cursor_set, parse_mentions,
)


class TeamchatSendExecutor:
    """Tool-side handle for ``teamchat_send``.

    Args:
        team_core_dir:     ``sessions/<team_id>/core/`` — owns teamchat.jsonl
        team_system_dir:   ``_sessions/<team_id>/`` — where we append a
                           UI-visible user_input row attributed to the sender
        member_name:       This member's in-team handle
        member_modes:      Mapping ``{name: "default" | "silent"}``
        members_map:       Mapping ``{name: child_session_id}`` for fan-out
        system_base:       Root of ``_sessions/`` — child system dirs are
                           reconstructed as ``system_base / child_id``
    """

    def __init__(
        self,
        *,
        team_core_dir: Path,
        team_system_dir: Path,
        member_name: str,
        member_modes: dict[str, str],
        members_map: dict[str, str],
        system_base: Path,
    ) -> None:
        self._team_core = Path(team_core_dir)
        self._team_system = Path(team_system_dir)
        self._me = member_name
        self._modes = dict(member_modes)
        self._members_map = dict(members_map)
        self._system_base = Path(system_base)
        self._chat = TeamChat(self._team_core, list(self._members_map.keys()))

    async def execute(self, **kwargs: Any) -> str:
        text = kwargs.get("text")
        if not isinstance(text, str) or not text.strip():
            return "Error: teamchat_send requires non-empty 'text'."

        # Step 1: persist
        msg = self._chat.post(self._me, text.strip())
        mentions = list(msg.mentions)

        # Step 2: mirror into the team session's UI feed so the human sees
        # the post immediately. caller=<member_name> + source=teamchat
        # tells the frontend to render this as an attributed group-chat
        # cell rather than a generic "user typed" cell.
        ts = datetime.now().isoformat()
        ui_event = {
            "type": "user_input",
            "content": text.strip(),
            "id": str(uuid4()),
            "ts": ts,
            "caller": self._me,
            "source": "teamchat",
            "teamchat_seq": msg.seq,
            "mentions": mentions,
        }
        try:
            with (self._team_system / "context.jsonl").open(
                "a", encoding="utf-8"
            ) as f:
                f.write(json.dumps(ui_event, ensure_ascii=False) + "\n")
        except OSError:
            pass

        # Step 3: fan out to peers
        delivered: list[str] = []
        deferred: list[str] = []
        addresses_all = "all" in mentions
        for name, sid in self._members_map.items():
            if name == self._me:
                continue
            mode = self._modes.get(name, "default")
            mentioned = addresses_all or (name in mentions)
            should_interrupt = (mode == "default") or mentioned
            if not should_interrupt:
                deferred.append(name)
                continue
            recipient_sys_dir = self._system_base / sid
            unread = self._chat.messages_since(cursor_get(recipient_sys_dir))
            summary = self._chat.summary_lines(unread, viewer=name)
            mention_label = "@all" if addresses_all else f"@{name}"
            header = (
                f"[teamchat] {mention_label} from @{self._me}\n"
                f"> {text.strip()}"
            )
            payload = (
                header if not summary else f"{header}\n\n{summary}"
            )
            try:
                BridgeSession(recipient_sys_dir).send_message(
                    payload,
                    caller="teamchat",
                    mode="interrupt",
                )
                cursor_set(recipient_sys_dir, msg.seq)
                delivered.append(name)
            except Exception as exc:  # noqa: BLE001 — best-effort
                deferred.append(f"{name} (delivery error: {exc})")

        # Tool result string — short, deterministic shape
        parts = [f"[teamchat_send seq={msg.seq}]"]
        if delivered:
            parts.append(f"interrupted: {', '.join(delivered)}")
        if deferred:
            parts.append(f"queued (will see on natural wakeup): {', '.join(deferred)}")
        if not delivered and not deferred:
            parts.append("no peers to notify")
        return " · ".join(parts)


# Generic loader hook — when nobody constructs a custom instance, the
# fallback path in ToolLoader looks for ``Executor`` here. A bare
# instance is useless without context, so we leave it absent.


# Mark this tool as needing context-injected construction. ToolLoader
# checks this attribute before falling back to ``Executor()`` / the
# generic ``execute()`` lookup.
NEEDS_TEAM_CONTEXT = True
