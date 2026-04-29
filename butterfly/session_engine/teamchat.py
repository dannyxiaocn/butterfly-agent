"""Teamchat — append-only group chat backing an AgentTeam session.

Layout (under the team's `sessions/<team_id>/core/`):

    teamchat.jsonl                ← canonical log; one message per line
                                    {seq, ts, sender, text, mentions}

Each member tracks how far they've read in
``_sessions/<member_session_id>/teamchat_cursor.json`` ({"seq": N}).

Public surface:

  TeamChat
    .post(sender, text)                       → GroupMessage
    .messages_since(seq)                      → list[GroupMessage]
    .latest_seq()                             → int
    .summary_lines(messages, *, viewer)       → str
    .messages_for_viewer(messages, *, viewer,
                         viewer_mode)         → list[GroupMessage]

  cursor_get(member_system_dir)               → int
  cursor_set(member_system_dir, seq)          → None

  parse_mentions(text, member_names)          → list[str]   ("all" possible)

The mention parser only understands the literal `@name` / `@all` forms
inside the message text — there is no separate ``mentions`` field on the
tool. Names are matched case-insensitively against the team's member list;
unknown ``@something`` tokens are ignored so a paragraph containing an
``user@example.com`` email is not interpreted as a mention.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path


_TEAMCHAT_FILE = "teamchat.jsonl"
_CURSOR_FILE = "teamchat_cursor.json"
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)")
_ALL_TOKEN = "all"


# ── data ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GroupMessage:
    seq: int
    ts: float
    sender: str
    text: str
    mentions: tuple[str, ...]   # may include "all"

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "sender": self.sender,
            "text": self.text,
            "mentions": list(self.mentions),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GroupMessage":
        mentions = data.get("mentions") or []
        if not isinstance(mentions, list):
            mentions = []
        return cls(
            seq=int(data.get("seq", 0)),
            ts=float(data.get("ts", 0.0)),
            sender=str(data.get("sender", "")),
            text=str(data.get("text", "")),
            mentions=tuple(str(m) for m in mentions),
        )


def parse_mentions(text: str, member_names: list[str]) -> list[str]:
    """Extract @-mentions from `text`.

    Returns a deduplicated list preserving first-seen order. Tokens that
    don't match a known member name (case-insensitive) AND aren't ``all``
    are dropped. ``all`` is returned as-is and intentionally takes priority
    when present (callers can short-circuit fan-out by checking
    ``"all" in mentions``).
    """
    name_lookup = {n.lower(): n for n in member_names}
    seen: set[str] = set()
    out: list[str] = []
    for raw in _MENTION_RE.findall(text):
        norm = raw.lower()
        if norm == _ALL_TOKEN:
            if _ALL_TOKEN not in seen:
                seen.add(_ALL_TOKEN)
                out.append(_ALL_TOKEN)
            continue
        if norm in name_lookup:
            canon = name_lookup[norm]
            if canon not in seen:
                seen.add(canon)
                out.append(canon)
    return out


# ── persistence ──────────────────────────────────────────────────────────────


class TeamChat:
    """Append-only teamchat backed by ``teamchat.jsonl``.

    Construction is cheap (no IO). Reads / writes happen lazily through the
    method calls. Multiple processes are expected to share the same file —
    each ``post()`` re-reads the trailing seq under the same open() so two
    concurrent posts don't collide on the same seq number. Race-correctness
    is best-effort at this layer (the BackgroundTaskManager + tool-loader
    don't issue concurrent writes from a single team in v1).
    """

    def __init__(self, core_dir: Path, member_names: list[str]) -> None:
        self.core_dir = Path(core_dir)
        self.path = self.core_dir / _TEAMCHAT_FILE
        self._member_names = list(member_names)

    # — write — — — — — — — — — — — — — — — — — — — — — — — — — — — — —

    def post(self, sender: str, text: str) -> GroupMessage:
        """Append a message. Mentions are parsed from the text body."""
        self.core_dir.mkdir(parents=True, exist_ok=True)
        mentions = parse_mentions(text, self._member_names)
        next_seq = self.latest_seq() + 1
        msg = GroupMessage(
            seq=next_seq,
            ts=time.time(),
            sender=sender,
            text=text,
            mentions=tuple(mentions),
        )
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")
        return msg

    # — read — — — — — — — — — — — — — — — — — — — — — — — — — — — — —

    def all_messages(self) -> list[GroupMessage]:
        if not self.path.exists():
            return []
        out: list[GroupMessage] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(GroupMessage.from_dict(json.loads(line)))
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
        return out

    def latest_seq(self) -> int:
        msgs = self.all_messages()
        return msgs[-1].seq if msgs else 0

    def messages_since(self, seq: int) -> list[GroupMessage]:
        return [m for m in self.all_messages() if m.seq > seq]

    # — viewer-aware filtering — — — — — — — — — — — — — — — — — — — —

    @staticmethod
    def is_visible_to(msg: GroupMessage, viewer: str) -> bool:
        """A message is visible to ``viewer`` whenever they didn't send it.

        v1 has no private messaging — every post is broadcast and everyone
        can read it via ``teamchat_view``. ``mentions`` only changes the
        wakeup behaviour, not visibility.
        """
        return msg.sender != viewer

    @staticmethod
    def addresses_viewer(msg: GroupMessage, viewer: str) -> bool:
        """True when the message @-mentions viewer or @all."""
        return _ALL_TOKEN in msg.mentions or viewer in msg.mentions

    # — formatting — — — — — — — — — — — — — — — — — — — — — — — — — —

    @staticmethod
    def summary_lines(
        messages: list[GroupMessage],
        *,
        viewer: str,
        since_label: str | None = None,
    ) -> str:
        """Render the brief teamchat status the recipient sees on wakeup.

        Format (matches the design doc):

            [teamchat] Unread since 14:22:01:
              • planner → 3 messages (1 @you)
              • coder   → 1 message
            Use teamchat_view() to read full content.

        ``viewer`` is the member whose perspective we render — ``@you`` =
        ``@viewer``. Returns the empty string when ``messages`` is empty.
        """
        if not messages:
            return ""
        per_sender: dict[str, list[GroupMessage]] = {}
        for m in messages:
            if m.sender == viewer:
                continue   # never list our own messages in our own summary
            per_sender.setdefault(m.sender, []).append(m)
        if not per_sender:
            return ""
        if since_label is None:
            earliest = min(messages, key=lambda m: m.ts).ts
            since_label = _fmt_ts(earliest)
        lines = [f"[teamchat] Unread since {since_label}:"]
        for sender, msgs in per_sender.items():
            n = len(msgs)
            tagged = sum(1 for m in msgs if TeamChat.addresses_viewer(m, viewer))
            tag = f" ({tagged} @you)" if tagged else ""
            word = "message" if n == 1 else "messages"
            lines.append(f"  • {sender} → {n} {word}{tag}")
        lines.append("Use teamchat_view() to read full content.")
        return "\n".join(lines)


# ── cursor (per-member read marker) ──────────────────────────────────────────


def cursor_path(member_system_dir: Path) -> Path:
    return Path(member_system_dir) / _CURSOR_FILE


def cursor_get(member_system_dir: Path) -> int:
    p = cursor_path(member_system_dir)
    if not p.exists():
        return 0
    try:
        return int(json.loads(p.read_text(encoding="utf-8")).get("seq", 0))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return 0


def cursor_set(member_system_dir: Path, seq: int) -> None:
    p = cursor_path(member_system_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"seq": int(seq)}), encoding="utf-8")
    tmp.replace(p)


# ── helpers ──────────────────────────────────────────────────────────────────


def _fmt_ts(ts: float) -> str:
    """Short HH:MM:SS for summary readability."""
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def format_messages_for_view(messages: list[GroupMessage]) -> str:
    """Render ordered messages as the body of a ``teamchat_view`` reply.

    Empty ``messages`` returns a fixed "no unread" string so the agent gets
    a deterministic shape it can pattern-match on.
    """
    if not messages:
        return "[teamchat] No unread messages."
    lines: list[str] = []
    for m in messages:
        ts = _fmt_ts(m.ts)
        mentions_part = ""
        if m.mentions:
            mentions_part = " " + " ".join(f"@{x}" for x in m.mentions)
        lines.append(f"[{ts}] {m.sender}{mentions_part}: {m.text}")
    return "\n".join(lines)
