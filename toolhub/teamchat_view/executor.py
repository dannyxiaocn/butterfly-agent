"""Executor for ``teamchat_view`` — return unread teamchat messages.

Returns the unread message bodies in chronological order and bumps the
caller's cursor to the latest seq it observed. Construction is identical
to ``teamchat_send`` — the loader builds an instance with context-injected
team paths the moment it loads tools for a member session.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from butterfly.session_engine.teamchat import (
    TeamChat, cursor_get, cursor_set, format_messages_for_view,
)


class TeamchatViewExecutor:
    def __init__(
        self,
        *,
        team_core_dir: Path,
        member_system_dir: Path,
        member_name: str,
        members_map: dict[str, str],
    ) -> None:
        self._team_core = Path(team_core_dir)
        self._member_sys_dir = Path(member_system_dir)
        self._me = member_name
        self._members_map = dict(members_map)
        self._chat = TeamChat(self._team_core, list(self._members_map.keys()))

    async def execute(self, **_: Any) -> str:
        cursor = cursor_get(self._member_sys_dir)
        unread = [
            m for m in self._chat.messages_since(cursor)
            if TeamChat.is_visible_to(m, self._me)
        ]
        rendered = format_messages_for_view(unread)
        if unread:
            cursor_set(self._member_sys_dir, unread[-1].seq)
        return rendered


NEEDS_TEAM_CONTEXT = True
