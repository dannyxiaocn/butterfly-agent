"""nutshell friends — IM-style session list with online/idle/offline status."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Status classification ─────────────────────────────────────────────────────

_ONLINE_THRESHOLD_SECS = 5 * 60       # 5 minutes
_IDLE_THRESHOLD_SECS = 60 * 60         # 1 hour


def classify_status(info: dict[str, Any]) -> str:
    """Classify a session as 'online', 'idle', or 'offline'.

    Rules:
    - **offline**: status == 'stopped' (always, regardless of last_run_at)
    - **online**: model_state == 'running' OR last_run_at within 5 minutes
    - **idle**: last_run_at within 5 min – 1 hour
    - **offline**: last_run_at > 1 hour or missing
    """
    if info.get("status") == "stopped":
        return "offline"

    if info.get("model_state") == "running":
        return "online"

    last_run = info.get("last_run_at")
    if not last_run:
        return "offline"

    try:
        dt = datetime.fromisoformat(last_run)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_secs = (datetime.now(tz=timezone.utc) - dt).total_seconds()
    except Exception:
        return "offline"

    if age_secs <= _ONLINE_THRESHOLD_SECS:
        return "online"
    if age_secs <= _IDLE_THRESHOLD_SECS:
        return "idle"
    return "offline"


# ── Time formatting ───────────────────────────────────────────────────────────

def _fmt_last(ts: str | None) -> str:
    """Format ISO timestamp as 'Xm ago' etc., or '—' if absent."""
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = int((datetime.now(tz=timezone.utc) - dt).total_seconds())
        if secs < 0:
            return "just now"
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return "—"


# ── Dot indicator ─────────────────────────────────────────────────────────────

_STATUS_DOT = {
    "online":  "●",
    "idle":    "◐",
    "offline": "○",
}


def _parse_ts(s: str | None) -> float:
    """Parse ISO timestamp to epoch float; return 0 on failure."""
    if not s:
        return 0.0
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


# ── Public API ────────────────────────────────────────────────────────────────

def build_friends_list(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enrich session dicts with 'friend_status' and 'last_ago' fields."""
    friends = []
    for s in sessions:
        status = classify_status(s)
        last_ts = s.get("last_run_at") or s.get("created_at")
        friends.append({
            **s,
            "friend_status": status,
            "last_ago": _fmt_last(last_ts),
        })
    # Sort: online first, then idle, then offline; within group by recency
    order = {"online": 0, "idle": 1, "offline": 2}
    friends.sort(key=lambda f: (
        order.get(f["friend_status"], 3),
        -_parse_ts(f.get("last_run_at") or f.get("created_at")),
    ))
    return friends


def format_friends_table(friends: list[dict[str, Any]]) -> str:
    """Format friends list as an IM-style contact list string."""
    if not friends:
        return "No sessions found."

    lines = []
    for f in friends:
        dot = _STATUS_DOT.get(f["friend_status"], "?")
        entity = f.get("entity", "?")
        sid = f.get("id", "?")
        status = f["friend_status"]
        last = f["last_ago"]
        lines.append(f"{dot} {entity:<16} ({sid})  {status:<8} last: {last}")
    return "\n".join(lines)


def format_friends_json(friends: list[dict[str, Any]]) -> str:
    """Format friends list as JSON, selecting only relevant fields."""
    compact = []
    for f in friends:
        compact.append({
            "id": f.get("id"),
            "entity": f.get("entity"),
            "status": f["friend_status"],
            "last_ago": f["last_ago"],
            "last_run_at": f.get("last_run_at"),
            "model_state": f.get("model_state"),
        })
    return json.dumps(compact, ensure_ascii=False, indent=2)
