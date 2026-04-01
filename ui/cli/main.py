"""nutshell — unified CLI for the Nutshell agent runtime.

Usage:
    nutshell chat MESSAGE [options]          Send a message / create a session
    nutshell sessions [--json]              List all sessions
    nutshell new [SESSION_ID] [options]     Create a new session (no message)
    nutshell stop SESSION_ID                Stop a session's heartbeat
    nutshell start SESSION_ID               Resume a stopped session
    nutshell log [SESSION_ID] [-n N] [--since T] [--watch]  Show conversation history
    nutshell tasks [SESSION_ID]             Show a session's task board
    nutshell entity new [options]           Scaffold a new entity directory
    nutshell entity log NAME                Show entity version changelog
    nutshell prompt-stats [SESSION_ID]      Show prompt space breakdown for a session
    nutshell token-report [SESSION_ID]      Show per-turn token usage breakdown
    nutshell repo-skill REPO_PATH           Generate codebase overview skill
    nutshell friends [--json]                IM-style session list with status
    nutshell review                         Review pending entity update requests
    nutshell server                         Start the Nutshell server
    nutshell web                            Start the web UI (monitoring)
    nutshell os [MESSAGE]                    CLI-OS playground session

All session-management commands (sessions, new, stop, start, tasks) work without
a running server — they read/write the _sessions/ directory directly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_SESSIONS_BASE = _REPO_ROOT / "sessions"
_DEFAULT_SYSTEM_BASE = _REPO_ROOT / "_sessions"


# ── inject-memory helpers ─────────────────────────────────────────────────────

def _parse_inject_memory(raw: list[str] | None) -> dict[str, str]:
    """Parse --inject-memory KEY=VALUE / KEY=@FILE items into {key: content}.

    Raises SystemExit on bad format or missing file.
    """
    if not raw:
        return {}
    result: dict[str, str] = {}
    for item in raw:
        eq = item.find("=")
        if eq < 1:
            print(f"Error: invalid --inject-memory format: {item!r}  (expected KEY=VALUE or KEY=@FILE)",
                  file=sys.stderr)
            sys.exit(2)
        key = item[:eq]
        value = item[eq + 1:]
        if value.startswith("@"):
            fpath = Path(value[1:])
            if not fpath.exists():
                print(f"Error: --inject-memory file not found: {fpath}", file=sys.stderr)
                sys.exit(2)
            value = fpath.read_text(encoding="utf-8")
        result[key] = value
    return result


def _write_inject_memory(session_dir: Path, memories: dict[str, str]) -> None:
    """Write memory layers to sessions/<id>/core/memory/<key>.md.

    Overwrites any existing file with the same key (intentional: caller explicitly
    wants this content injected).
    """
    if not memories:
        return
    mem_dir = session_dir / "core" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    for key, value in memories.items():
        (mem_dir / f"{key}.md").write_text(value, encoding="utf-8")



# ── Helpers ───────────────────────────────────────────────────────────────────

def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _fmt_ago(ts: str | None) -> str:
    """Format ISO timestamp as 'Xm ago' / 'Xh ago' / 'Xd ago', or '' if absent."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        secs = int((now - dt).total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return ""


def _session_tone(info: dict) -> str:
    if info.get("pid_alive") and info.get("model_state") == "running" and info.get("status") != "stopped":
        return "running"
    if info.get("has_tasks") and info.get("pid_alive") and info.get("status") != "stopped":
        return "napping"
    if info.get("status") == "stopped":
        return "stopped"
    return "idle"


def _read_all_sessions(
    sessions_base: Path,
    system_base: Path,
) -> list[dict]:
    """Read all sessions from _sessions/ + sessions/. No server required."""
    from ui.web.sessions import _read_session_info, _sort_sessions
    results = []
    if not system_base.is_dir():
        return []
    for system_dir in sorted(system_base.iterdir()):
        if not system_dir.is_dir():
            continue
        session_dir = sessions_base / system_dir.name
        info = _read_session_info(session_dir, system_dir)
        if info:
            results.append(info)
    return _sort_sessions(results)


# ── Subcommand: chat ──────────────────────────────────────────────────────────

def _add_chat_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "chat",
        help="Send a message to a session and print the response.",
        description=(
            "Send a message to an existing session or create a new one.\n\n"
            "Examples:\n"
            "  nutshell chat 'Plan a data pipeline'\n"
            "  nutshell chat --entity kimi_agent 'Review this code'\n"
            "  nutshell chat --session 2026-03-25_10-00-00 'Status?'\n"
            "  nutshell chat --session <id> --no-wait 'Run overnight'\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("message", help="Message to send")
    p.add_argument("--session", metavar="ID", help="Continue an existing session")
    p.add_argument("--entity", default="agent", metavar="NAME",
                   help="Entity for new session (default: agent)")
    p.add_argument("--no-wait", action="store_true", help="Fire-and-forget")
    p.add_argument("--timeout", type=float, default=300.0,
                   help="Seconds to wait for a response (default: 300)")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--keep-alive", action="store_true", default=False,
                   dest="keep_alive",
                   help="Keep the server running in background after reply")
    p.add_argument("--inject-memory", action="append", metavar="KEY=VALUE",
                   dest="inject_memory",
                   help="Inject a memory layer: KEY=VALUE or KEY=@FILE (repeatable)")
    p.set_defaults(func=cmd_chat)


def cmd_chat(args) -> int:
    from ui.cli.chat import _continue_session, _new_session
    inject = _parse_inject_memory(getattr(args, "inject_memory", None))
    if args.session:
        if inject:
            # Write injected memory to existing session
            session_dir = args.sessions_base / args.session
            if session_dir.exists():
                _write_inject_memory(session_dir, inject)
        return _continue_session(
            args.session, args.message,
            no_wait=args.no_wait, timeout=args.timeout,
            system_base=args.system_base,
        )
    return _new_session(
        args.entity, args.message,
        no_wait=args.no_wait, timeout=args.timeout,
        system_base=args.system_base,
        sessions_base=args.sessions_base,
        inject_memory=inject,
        keep_alive=getattr(args, 'keep_alive', False),
    )


# ── Subcommand: sessions ──────────────────────────────────────────────────────

def _add_sessions_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "sessions",
        help="List all sessions.",
        description="List all sessions with status, entity, and last-run time.",
    )
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="Output as JSON array (useful for agents)")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_sessions)


def cmd_sessions(args) -> int:
    sessions = _read_all_sessions(args.sessions_base, args.system_base)

    if args.as_json:
        print(json.dumps(sessions, ensure_ascii=False, indent=2))
        return 0

    if not sessions:
        print("No sessions found.")
        return 0

    # Table layout
    COL = {"id": 26, "entity": 16, "status": 10, "last_run": 10}
    header = (
        f"{'ID':<{COL['id']}}  {'ENTITY':<{COL['entity']}}  "
        f"{'STATUS':<{COL['status']}}  LAST RUN"
    )
    print(header)
    print("─" * (sum(COL.values()) + 6))
    for s in sessions:
        tone = _session_tone(s)
        status_label = {
            "running": "running",
            "napping": "napping",
            "stopped": "stopped",
            "idle":    "idle",
        }.get(tone, tone)
        last_run = _fmt_ago(s.get("last_run_at")) or _fmt_ago(s.get("created_at")) or "—"
        print(
            f"{s['id']:<{COL['id']}}  {s.get('entity','?'):<{COL['entity']}}  "
            f"{status_label:<{COL['status']}}  {last_run}"
        )
    return 0


# ── Subcommand: new ───────────────────────────────────────────────────────────

def _add_new_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "new",
        help="Create a new session (no message — use 'chat' to send immediately).",
        description=(
            "Create a session from an entity. Session ID is auto-generated from\n"
            "the current timestamp unless specified explicitly.\n\n"
            "Examples:\n"
            "  nutshell new\n"
            "  nutshell new --entity kimi_agent\n"
            "  nutshell new my-project --entity agent\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("session_id", nargs="?", default=None,
                   help="Session ID (default: current timestamp)")
    p.add_argument("--entity", default="agent", metavar="NAME",
                   help="Entity to initialise from (default: agent)")
    p.add_argument("--heartbeat", type=float, default=600.0,
                   help="Heartbeat interval in seconds (default: 600)")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--inject-memory", action="append", metavar="KEY=VALUE",
                   dest="inject_memory",
                   help="Inject a memory layer: KEY=VALUE or KEY=@FILE (repeatable)")
    p.set_defaults(func=cmd_new)


def cmd_new(args) -> int:
    from nutshell.runtime.session_factory import init_session
    session_id = args.session_id or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    entity_dir = _REPO_ROOT / "entity" / args.entity
    if not entity_dir.exists():
        print(f"Error: entity '{args.entity}' not found in entity/", file=sys.stderr)
        return 1
    try:
        init_session(
            session_id=session_id,
            entity_name=args.entity,
            sessions_base=args.sessions_base,
            system_sessions_base=args.system_base,
            heartbeat=args.heartbeat,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    inject = _parse_inject_memory(getattr(args, "inject_memory", None))
    if inject:
        _write_inject_memory(args.sessions_base / session_id, inject)
    print(session_id)
    return 0


# ── Subcommand: stop ──────────────────────────────────────────────────────────

def _add_stop_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "stop",
        help="Stop a session's heartbeat.",
    )
    p.add_argument("session_id", help="Session ID to stop")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_stop)


def cmd_stop(args) -> int:
    from nutshell.runtime.status import write_session_status
    system_dir = args.system_base / args.session_id
    if not (system_dir / "manifest.json").exists():
        print(f"Error: session '{args.session_id}' not found", file=sys.stderr)
        return 1
    write_session_status(system_dir, status="stopped",
                         stopped_at=datetime.now().isoformat())
    from nutshell.runtime.ipc import FileIPC
    FileIPC(system_dir).append_event(
        {"type": "status", "value": "stopped via CLI"}
    )
    print(f"Stopped: {args.session_id}")
    return 0


# ── Subcommand: start ─────────────────────────────────────────────────────────

def _add_start_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "start",
        help="Resume a stopped session (requires server to be running).",
    )
    p.add_argument("session_id", help="Session ID to resume")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_start)


def cmd_start(args) -> int:
    from nutshell.runtime.status import write_session_status
    system_dir = args.system_base / args.session_id
    if not (system_dir / "manifest.json").exists():
        print(f"Error: session '{args.session_id}' not found", file=sys.stderr)
        return 1
    write_session_status(system_dir, status="active", stopped_at=None)
    from nutshell.runtime.ipc import FileIPC
    FileIPC(system_dir).append_event(
        {"type": "status", "value": "resumed via CLI"}
    )
    print(f"Started: {args.session_id}")
    return 0


# ── Subcommand: log ───────────────────────────────────────────────────────────


# ── Helpers for --since / --watch ─────────────────────────────────────────────

def _parse_since(value: str) -> float:
    """Parse a --since value into a UNIX timestamp (float).

    Accepted formats:
      - 'now'                         → current time
      - ISO-8601: '2026-03-25T12:00:00' → that moment (local TZ)
      - UNIX timestamp string: '1742900400' → that epoch
    Raises ValueError for anything else.
    """
    if value == "now":
        import time
        return time.time()
    # Try ISO-8601
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(value)
        return dt.timestamp()
    except (ValueError, TypeError):
        pass
    # Try bare UNIX timestamp
    try:
        ts = float(value)
        if ts > 1_000_000_000:  # sanity: after 2001
            return ts
    except (ValueError, TypeError):
        pass
    raise ValueError(f"Cannot parse --since value: {value!r}. Use 'now', an ISO-8601 datetime, or a UNIX timestamp.")


def _turn_ts(turn: dict) -> float | None:
    """Extract UNIX timestamp from a turn/event dict. Returns None if missing."""
    raw = turn.get("ts")
    if raw is None:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(raw).timestamp()
    except (ValueError, TypeError):
        return None


def _load_context(path) -> tuple[dict, list]:
    """Load context.jsonl → (inputs_by_id, turns)."""
    import json
    from pathlib import Path
    lines = [l for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    inputs_by_id: dict[str, dict] = {}
    turns: list[dict] = []
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "user_input":
            inputs_by_id[ev["id"]] = ev
        elif ev.get("type") == "turn":
            turns.append(ev)
    return inputs_by_id, turns

def _add_log_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "log",
        help="Show recent conversation history for a session.",
        description=(
            "Display the last N conversation turns from a session.\n\n"
            "Examples:\n"
            "  nutshell log                                  Show latest session, last 5 turns\n"
            "  nutshell log 2026-03-25_10-00-00              Specific session\n"
            "  nutshell log -n 20                            Last 20 turns\n"
            "  nutshell log --since now                      Bookmark 'now', future calls show new turns only\n"
            "  nutshell log --since 2026-03-25T12:00:00      Turns after a specific time\n"
            "  nutshell log --watch                          Poll every 2s for new turns (Ctrl-C to stop)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("session_id", nargs="?", default=None,
                   help="Session ID (default: most recently active session)")
    p.add_argument("-n", type=int, default=5, dest="num_turns",
                   metavar="N", help="Number of turns to show (default: 5)")
    p.add_argument("--since", type=str, default=None, metavar="TIMESTAMP",
                   help="Only show turns after this time (ISO-8601, UNIX epoch, or 'now')")
    p.add_argument("--watch", action="store_true", default=False,
                   help="Poll for new turns every 2 seconds (implies --since now if --since not set)")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_log)


def _fmt_msg_content(content) -> str:
    """Flatten message content to a display string (handles str and list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool: {block.get('name', '?')}({json.dumps(block.get('input', {}), ensure_ascii=False)})]")
                elif block.get("type") == "tool_result":
                    inner = block.get("content", "")
                    preview = (inner[:80] + "…") if isinstance(inner, str) and len(inner) > 80 else inner
                    parts.append(f"[result: {preview}]")
        return " ".join(p for p in parts if p)
    return str(content)


def cmd_log(args) -> int:
    session_id = args.session_id

    if not session_id:
        sessions = _read_all_sessions(args.sessions_base, args.system_base)
        if not sessions:
            print("No sessions found.", file=sys.stderr)
            return 1
        session_id = sessions[0]["id"]

    context_path = args.system_base / session_id / "context.jsonl"
    if not context_path.exists():
        if not (args.system_base / session_id / "manifest.json").exists():
            print(f"Error: session '{session_id}' not found", file=sys.stderr)
            return 1
        print(f"[{session_id}] No conversation history yet.")
        return 0

    # Parse --since threshold
    since_ts: float | None = None
    since_raw = getattr(args, "since", None)
    watch_mode = getattr(args, "watch", False)

    if watch_mode and since_raw is None:
        since_raw = "now"  # --watch implies --since now

    if since_raw is not None:
        since_ts = _parse_since(since_raw)

    if watch_mode:
        return _watch_log(args, session_id, context_path, since_ts)

    # Single-shot mode
    inputs_by_id, turns = _load_context(context_path)

    if since_ts is not None:
        turns = [t for t in turns if (_turn_ts(t) or 0) > since_ts]

    # Apply -n limit (only when not using --since)
    if since_ts is None:
        turns_to_show = turns[-args.num_turns:]
    else:
        turns_to_show = turns  # show ALL turns after --since

    if not turns_to_show:
        if since_ts is not None:
            print(f"[{session_id}] No new turns since {since_raw}.")
        else:
            # Show any unpaired user_inputs
            recent_inputs = list(inputs_by_id.values())[-args.num_turns:]
            if recent_inputs:
                print(f"[{session_id}] — pending (no agent response yet)")
                print("─" * 60)
                for inp in recent_inputs:
                    ts = inp.get("ts", "")[:16].replace("T", " ")
                    print(f"  USER  {ts}  {inp.get('content', '')}")
            else:
                print(f"[{session_id}] No conversation history yet.")
        return 0

    print(f"[{session_id}] {len(turns_to_show)} turn(s)" + (f" since {since_raw}" if since_ts else ""))
    print("─" * 60)
    _print_turns(turns_to_show, inputs_by_id)
    return 0


def _watch_log(args, session_id: str, context_path, since_ts: float) -> int:
    """Poll context.jsonl for new turns every 2 seconds."""
    import time

    cursor = since_ts
    print(f"[{session_id}] watching for new turns (Ctrl-C to stop) …")
    try:
        while True:
            if context_path.exists():
                inputs_by_id, turns = _load_context(context_path)
                new_turns = [t for t in turns if (_turn_ts(t) or 0) > cursor]
                if new_turns:
                    _print_turns(new_turns, inputs_by_id)
                    # Advance cursor to latest turn
                    latest = max((_turn_ts(t) or 0) for t in new_turns)
                    if latest > cursor:
                        cursor = latest
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n[watch stopped]")
        return 0


def _print_turns(turns: list[dict], inputs_by_id: dict[str, dict]) -> None:
    """Print a list of turns with their associated user inputs."""
    for turn in turns:
        uid = turn.get("user_input_id")
        user_ev = inputs_by_id.get(uid) if uid else None
        ts = (user_ev or turn).get("ts", "")[:16].replace("T", " ")

        # User line
        user_text = user_ev.get("content", "") if user_ev else ""
        if user_text:
            print(f"  USER  {ts}  {user_text}")

        # Agent messages (skip the echoed user message)
        messages = turn.get("messages", [])
        for msg in messages:
            role = msg.get("role", "")
            if role == "assistant":
                text = _fmt_msg_content(msg.get("content", ""))
                if text:
                    print(f"  AGENT          {text}")
            elif role == "tool":
                pass  # skip tool results in display

        # Token usage
        usage = turn.get("usage")
        if usage and (usage.get("input") or usage.get("output")):
            parts = []
            if usage.get("input"):
                parts.append(f"↑{usage['input']}")
            if usage.get("output"):
                parts.append(f"↓{usage['output']}")
            if usage.get("cache_read"):
                parts.append(f"📦{usage['cache_read']}")
            print(f"         {'  '.join(parts)}")
        print()


# ── Subcommand: token-report ──────────────────────────────────────────────────

def _add_token_report_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "token-report",
        help="Show per-turn token usage breakdown for a session.",
        description=(
            "Display token costs per turn with totals and cache efficiency.\n\n"
            "Examples:\n"
            "  nutshell token-report                       Latest session\n"
            "  nutshell token-report 2026-03-25_10-00-00   Specific session\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("session_id", nargs="?", default=None,
                   help="Session ID (default: most recently active session)")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_token_report)


def cmd_token_report(args) -> int:
    session_id = args.session_id
    if not session_id:
        sessions = _read_all_sessions(args.sessions_base, args.system_base)
        if not sessions:
            print("No sessions found.", file=sys.stderr)
            return 1
        session_id = sessions[0]["id"]

    context_path = args.system_base / session_id / "context.jsonl"
    if not context_path.exists():
        if not (args.system_base / session_id / "manifest.json").exists():
            print(f"Error: session '{session_id}' not found", file=sys.stderr)
            return 1
        print(f"[{session_id}] No conversation history yet.")
        return 0

    lines = [l for l in context_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    inputs_by_id: dict[str, dict] = {}
    turns: list[dict] = []
    for ev in events:
        if ev.get("type") == "user_input":
            inputs_by_id[ev["id"]] = ev
        elif ev.get("type") == "turn":
            turns.append(ev)

    if not turns:
        print(f"[{session_id}] No turns with token data yet.")
        return 0

    # Collect per-turn data
    rows = []
    for i, turn in enumerate(turns, 1):
        usage = turn.get("usage") or {}
        inp = usage.get("input", 0) or 0
        out = usage.get("output", 0) or 0
        cr = usage.get("cache_read", 0) or 0
        cw = usage.get("cache_write", 0) or 0
        uid = turn.get("user_input_id")
        user_ev = inputs_by_id.get(uid) if uid else None
        ts = (user_ev or turn).get("ts", "")[:16].replace("T", " ")
        trigger = ""
        if user_ev:
            raw = user_ev.get("content", "")
            trigger = (raw[:40] + "…") if len(raw) > 40 else raw
        elif turn.get("pre_triggered"):
            trigger = "[heartbeat]"
        rows.append((i, ts, trigger, inp, out, cr, cw))

    # Column widths
    W = (4, 16, 42, 8, 8, 8, 8)
    header = (
        f"{'#':>{W[0]}}  {'Time':<{W[1]}}  {'Trigger':<{W[2]}}"
        f"  {'Input':>{W[3]}}  {'Output':>{W[4]}}  {'CacheR':>{W[5]}}  {'CacheW':>{W[6]}}"
    )
    sep = "─" * (sum(W) + 2 * 6)

    print(f"[{session_id}] token-report  ({len(rows)} turns)")
    print(sep)
    print(header)
    print(sep)
    for idx, ts, trigger, inp, out, cr, cw in rows:
        print(
            f"{idx:>{W[0]}}  {ts:<{W[1]}}  {trigger:<{W[2]}}"
            f"  {inp:>{W[3]}}  {out:>{W[4]}}  {cr:>{W[5]}}  {cw:>{W[6]}}"
        )
    print(sep)

    # Totals
    total_inp = sum(r[3] for r in rows)
    total_out = sum(r[4] for r in rows)
    total_cr  = sum(r[5] for r in rows)
    total_cw  = sum(r[6] for r in rows)
    print(
        f"{'TOT':>{W[0]}}  {'':>{W[1]}}  {'':>{W[2]}}"
        f"  {total_inp:>{W[3]}}  {total_out:>{W[4]}}  {total_cr:>{W[5]}}  {total_cw:>{W[6]}}"
    )

    # Cache efficiency
    total_billed = total_inp + total_out
    if total_billed > 0:
        cache_pct = total_cr * 100 // (total_inp + total_cr) if (total_inp + total_cr) > 0 else 0
        print()
        print(f"  Cache hit rate : {cache_pct}%  ({total_cr:,} read / {total_inp + total_cr:,} total input)")
        print(f"  Billed tokens  : {total_billed:,}  (input {total_inp:,} + output {total_out:,})")

        # Highlight the most expensive turns (top 3 by input)
        ranked = sorted(rows, key=lambda r: r[3] + r[4], reverse=True)[:3]
        if ranked and ranked[0][3] + ranked[0][4] > 0:
            print()
            print("  Most expensive turns (by input+output):")
            for idx, ts, trigger, inp, out, cr, cw in ranked:
                total = inp + out
                if total > 0:
                    print(f"    #{idx:>3}  {ts}  {inp+out:>8} tok  {trigger}")

    return 0


# ── Subcommand: tasks ─────────────────────────────────────────────────────────

def _add_tasks_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "tasks",
        help="Show a session's task board (core/tasks.md).",
        description=(
            "Display the task board for a session.\n\n"
            "Examples:\n"
            "  nutshell tasks                       Show latest session's tasks\n"
            "  nutshell tasks 2026-03-25_10-00-00   Show specific session\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("session_id", nargs="?", default=None,
                   help="Session ID (default: most recently active session)")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_tasks)


def cmd_tasks(args) -> int:
    session_id = args.session_id

    # Resolve session_id: if not given, use the most recently active session
    if not session_id:
        sessions = _read_all_sessions(args.sessions_base, args.system_base)
        if not sessions:
            print("No sessions found.", file=sys.stderr)
            return 1
        session_id = sessions[0]["id"]

    tasks_path = args.sessions_base / session_id / "core" / "tasks.md"
    if not tasks_path.exists():
        # Check if the session exists at all
        if not (args.system_base / session_id / "manifest.json").exists():
            print(f"Error: session '{session_id}' not found", file=sys.stderr)
            return 1
        print(f"[{session_id}] tasks.md is empty.")
        return 0

    content = tasks_path.read_text().strip()
    print(f"[{session_id}] tasks.md")
    print("─" * 60)
    if content:
        print(content)
    else:
        print("(empty)")
    return 0


# ── Subcommand: prompt-stats ──────────────────────────────────────────────────

_MEMORY_LAYER_INLINE_LINES = 60  # must match Agent._MEMORY_LAYER_INLINE_LINES


def _add_prompt_stats_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "prompt-stats",
        help="Show prompt space breakdown for a session.",
        description=(
            "Display a component-by-component breakdown of system prompt size.\n\n"
            "Examples:\n"
            "  nutshell prompt-stats                       Latest session\n"
            "  nutshell prompt-stats 2026-03-25_10-00-00   Specific session\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("session_id", nargs="?", default=None,
                   help="Session ID (default: most recently active session)")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_prompt_stats)


def _prompt_stats_row(label: str, content: str, note: str = "") -> tuple[str, int, int, int]:
    """Return (label, lines_disk, chars_prompt, tokens_est) for a prompt component."""
    lines = len(content.splitlines())
    chars = len(content)
    tokens = max(1, chars // 4)
    return (label, lines, chars, tokens, note)


def cmd_prompt_stats(args) -> int:
    session_id = args.session_id
    if not session_id:
        sessions = _read_all_sessions(args.sessions_base, args.system_base)
        if not sessions:
            print("No sessions found.", file=sys.stderr)
            return 1
        session_id = sessions[0]["id"]

    core = args.sessions_base / session_id / "core"
    if not core.exists():
        print(f"Error: session '{session_id}' not found", file=sys.stderr)
        return 1

    rows: list[tuple] = []  # (label, lines, chars, tokens, note)

    def _read(path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    # ── Static (cached) section ───────────────────────────────────────────────
    system_content = _read(core / "system.md")
    rows.append(_prompt_stats_row("system.md", system_content))

    session_content = _read(core / "session.md") or _read(core / "session_context.md")
    rows.append(_prompt_stats_row("session.md", session_content))

    # ── Dynamic section ───────────────────────────────────────────────────────
    memory_content = _read(core / "memory.md")
    rows.append(_prompt_stats_row("memory.md", memory_content))

    mem_dir = core / "memory"
    if mem_dir.exists():
        for md_file in sorted(mem_dir.glob("*.md")):
            content = md_file.read_text(encoding="utf-8")
            disk_lines = len(content.splitlines())
            if disk_lines > _MEMORY_LAYER_INLINE_LINES:
                # truncated in prompt
                truncated = "\n".join(content.splitlines()[:_MEMORY_LAYER_INLINE_LINES])
                note = f"truncated ({disk_lines}→{_MEMORY_LAYER_INLINE_LINES} lines)"
                rows.append(_prompt_stats_row(f"memory/{md_file.stem}", truncated, note))
            else:
                rows.append(_prompt_stats_row(f"memory/{md_file.stem}", content))

    # skills: count skills dir entries
    skills_dir = core / "skills"
    skill_names: list[str] = []
    if skills_dir.exists():
        skill_names = [d.name for d in sorted(skills_dir.iterdir()) if d.is_dir()]
    # Each skill contributes a catalog line (file-backed = catalog only, ~40 chars each)
    catalog_chars = sum(40 + len(n) for n in skill_names)
    skill_note = f"{len(skill_names)} skills (catalog only; bodies loaded on demand)"
    rows.append((
        "skills (catalog)",
        len(skill_names),
        catalog_chars,
        max(1, catalog_chars // 4),
        skill_note,
    ))

    # ── Heartbeat (separate activation) ──────────────────────────────────────
    hb_content = _read(core / "heartbeat.md")
    rows.append(_prompt_stats_row("heartbeat.md *", hb_content, "heartbeat activations only"))

    # ── Render table ──────────────────────────────────────────────────────────
    COL = (34, 7, 8, 8)
    header = f"{'Component':<{COL[0]}}  {'Lines':>{COL[1]}}  {'Chars':>{COL[2]}}  {'~Tokens':>{COL[3]}}  Note"
    sep = "─" * (sum(COL) + 10 + 40)

    print(f"[{session_id}] prompt-stats")
    print(sep)
    print(header)
    print(sep)

    # Group: Static
    print("  STATIC (cached)")
    static_rows = rows[:2]
    for label, lines, chars, tokens, note in static_rows:
        print(f"  {label:<{COL[0]}}  {lines:>{COL[1]}}  {chars:>{COL[2]}}  {tokens:>{COL[3]}}  {note}")

    # Group: Dynamic
    dynamic_rows = rows[2:-1]
    print("  DYNAMIC")
    for label, lines, chars, tokens, note in dynamic_rows:
        print(f"  {label:<{COL[0]}}  {lines:>{COL[1]}}  {chars:>{COL[2]}}  {tokens:>{COL[3]}}  {note}")

    # Group: Heartbeat
    print("  HEARTBEAT")
    label, lines, chars, tokens, note = rows[-1]
    print(f"  {label:<{COL[0]}}  {lines:>{COL[1]}}  {chars:>{COL[2]}}  {tokens:>{COL[3]}}  {note}")

    print(sep)

    # Totals (static + dynamic, excluding heartbeat)
    chat_rows = rows[:-1]
    total_chars = sum(r[2] for r in chat_rows)
    total_tokens = sum(r[3] for r in chat_rows)
    static_chars = sum(r[2] for r in static_rows)
    static_tokens = sum(r[3] for r in static_rows)
    dynamic_chars = sum(r[2] for r in dynamic_rows)
    dynamic_tokens = sum(r[3] for r in dynamic_rows)
    print(f"  {'TOTAL (chat)':<{COL[0]}}  {'':>{COL[1]}}  {total_chars:>{COL[2]}}  {total_tokens:>{COL[3]}}  static {static_tokens} + dynamic {dynamic_tokens}")
    print()
    print("  * heartbeat.md is injected during autonomous heartbeat ticks, not regular chat.")
    return 0


# ── Subcommand: entity ────────────────────────────────────────────────────────

def _add_entity_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "entity",
        help="Manage entity definitions.",
    )
    esub = p.add_subparsers(dest="entity_cmd", metavar="COMMAND")
    esub.required = True

    enew = esub.add_parser(
        "new",
        help="Scaffold a new entity directory.",
        description=(
            "Scaffold a new agent entity directory.\n\n"
            "Examples:\n"
            "  nutshell entity new                          # interactive\n"
            "  nutshell entity new -n my-agent\n"
            "  nutshell entity new -n my-agent --extends agent\n"
            "  nutshell entity new -n my-agent --standalone\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    enew.add_argument("-n", "--name", metavar="NAME", help="Entity name")
    enew.add_argument("--extends", metavar="PARENT",
                      help="Parent entity to inherit from (skips picker)")
    enew.add_argument("--standalone", action="store_true",
                      help="Create standalone entity with no inheritance")
    enew.add_argument("--entity-dir", default="entity", metavar="DIR",
                      help="Base directory for entities (default: entity/)")

    elog = esub.add_parser(
        "log",
        help="Show entity version changelog.",
        description=(
            "Show the version changelog for an entity.\n\n"
            "Examples:\n"
            "  nutshell entity log agent\n"
            "  nutshell entity log nutshell_dev\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    elog.add_argument("name", metavar="NAME", help="Entity name")
    elog.add_argument("--entity-dir", default="entity", metavar="DIR",
                      help=argparse.SUPPRESS)

    p.set_defaults(func=cmd_entity)


def cmd_entity(args) -> int:
    if args.entity_cmd == "new":
        from ui.cli.new_agent import _ask_name, _ask_parent, create_entity
        entity_dir = Path(args.entity_dir)
        name = args.name or _ask_name()
        if args.standalone:
            parent = None
        elif args.extends:
            parent = args.extends
        else:
            parent = _ask_parent(entity_dir)
        try:
            created = create_entity(name, entity_dir, parent)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Created: {created}/")
        if parent:
            print(f"  (extends '{parent}')")
        return 0

    if args.entity_cmd == "log":
        from nutshell.runtime.entity_updates import get_entity_version, get_entity_changelog
        entity_dir = Path(args.entity_dir)
        entity_path = entity_dir / args.name
        if not entity_path.exists():
            print(f"Error: entity '{args.name}' not found in {entity_dir}/", file=sys.stderr)
            return 1
        version = get_entity_version(args.name, repo_root=entity_dir.parent)
        changelog = get_entity_changelog(args.name, repo_root=entity_dir.parent)
        print(f"Entity: {args.name}  (v{version})")
        print("─" * 60)
        if changelog:
            print(changelog)
        else:
            print("(no changelog entries yet — changes are recorded when updates are applied)")
        return 0

    return 0


# ── Subcommand: review ────────────────────────────────────────────────────────

def _add_review_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "review",
        help="Review pending entity update requests from agents.",
    )
    p.add_argument("--list", action="store_true", help="List only, don't prompt")
    p.set_defaults(func=cmd_review)


def cmd_review(args) -> int:
    from ui.cli.review_updates import main as review_main
    # Inject --list flag if requested
    if args.list:
        sys.argv = ["nutshell-review-updates", "--list"]
    else:
        sys.argv = ["nutshell-review-updates"]
    review_main()
    return 0


# ── Subcommands: server / web ─────────────────────────────────────────────────

def _add_exec_parser(subparsers, name: str, help_text: str) -> None:
    p = subparsers.add_parser(name, help=help_text)
    p.set_defaults(func=lambda args: _exec_entrypoint(name))


def _exec_entrypoint(name: str) -> int:
    """Replace the current process with the named entry-point script."""
    import shutil
    cmd = f"nutshell-{name}"
    path = shutil.which(cmd)
    if path:
        os.execv(path, [path])
    # Fallback: call the Python module directly
    mapping = {
        "server": ("nutshell.runtime.server", "main"),
        "web":    ("ui.web", "main"),
    }
    if name in mapping:
        module_path, fn = mapping[name]
        import importlib
        mod = importlib.import_module(module_path)
        getattr(mod, fn)()
        return 0
    print(f"Error: unknown entrypoint '{name}'", file=sys.stderr)
    return 1







# ── Subcommand: kanban ────────────────────────────────────────────────────────

def _add_kanban_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "kanban",
        help="Unified task board — show tasks.md for all sessions.",
        description=(
            "Display every session's task board in one view.\n\n"
            "Examples:\n"
            "  nutshell kanban                      # all sessions\n"
            "  nutshell kanban --session ID          # single session\n"
            "  nutshell kanban --json                # JSON for agents\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--session", metavar="ID", default=None,
                   help="Show only this session")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="Output as JSON array")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_kanban)


def cmd_kanban(args) -> int:
    from ui.cli.kanban import build_kanban, format_kanban_table, format_kanban_json
    sessions = _read_all_sessions(
        sessions_base=args.sessions_base,
        system_base=args.system_base,
    )
    if args.session:
        sessions = [s for s in sessions if s.get("id") == args.session]
        if not sessions:
            print(f"Error: session '{args.session}' not found", file=sys.stderr)
            return 1
    entries = build_kanban(sessions, sessions_base=args.sessions_base)
    if args.as_json:
        print(format_kanban_json(entries))
    else:
        print(format_kanban_table(entries))
    return 0


# ── Subcommand: friends ───────────────────────────────────────────────────────

def _add_friends_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "friends",
        help="IM-style session list with online/idle/offline status.",
        description=(
            "Show all sessions as a contact list with live status indicators.\n\n"
            "Examples:\n"
            "  nutshell friends                     # pretty table\n"
            "  nutshell friends --json              # JSON for agents\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="Output as JSON array")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_friends)


def cmd_friends(args) -> int:
    from ui.cli.friends import build_friends_list, format_friends_table, format_friends_json
    sessions = _read_all_sessions(
        sessions_base=args.sessions_base,
        system_base=args.system_base,
    )
    friends = build_friends_list(sessions)
    if args.as_json:
        print(format_friends_json(friends))
    else:
        print(format_friends_table(friends))
    return 0


# ── Subcommand: repo-skill ────────────────────────────────────────────────────

def _add_repo_skill_parser(subparsers) -> None:
    p = subparsers.add_parser(
        'repo-skill',
        help='Generate a codebase overview skill from any repo.',
        description=(
            "Generate a SKILL.md codebase overview from a repository.\n\n"
            "Examples:\n"
            "  nutshell repo-skill ./my-project\n"
            "  nutshell repo-skill ~/code/fastapi --name fastapi\n"
            "  nutshell repo-skill . --output /tmp/skills/my-wiki\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('repo_path', metavar='REPO_PATH', help='Path to the repository')
    p.add_argument('--output', '-o', metavar='DIR',
                   help='Output directory (default: core/skills/<name>-wiki/ in current session)')
    p.add_argument('--name', '-n', metavar='NAME',
                   help='Skill name (default: repo directory name)')
    p.set_defaults(func=_cmd_repo_skill)


def _cmd_repo_skill(args) -> int:
    from ui.cli.repo_skill import cmd_repo_skill
    return cmd_repo_skill(args)


# ── Subcommand: repo-dev ──────────────────────────────────────────────────────

def _add_repo_dev_parser(subparsers) -> None:
    p = subparsers.add_parser(
        'repo-dev',
        help='Create a dedicated dev-agent session for any repo.',
        description=(
            "Create a dev-agent session pre-loaded with a codebase overview skill.\n\n"
            "Examples:\n"
            "  nutshell repo-dev ./my-project\n"
            "  nutshell repo-dev ~/code/fastapi --name fastapi\n"
            "  nutshell repo-dev . -m 'add unit tests for the parser module'\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('repo_path', metavar='REPO_PATH', help='Path to the repository')
    p.add_argument('--name', '-n', metavar='NAME',
                   help='Project name (default: repo directory name)')
    p.add_argument('--message', '-m', metavar='MSG',
                   help='Initial message to send to the dev agent')
    p.set_defaults(func=_cmd_repo_dev)


def _cmd_repo_dev(args) -> int:
    from ui.cli.repo_skill import cmd_repo_dev
    return cmd_repo_dev(args)



# ── Subcommand: visit ─────────────────────────────────────────────────────────

def _add_visit_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "visit",
        help="Agent room view — detailed status of a single session.",
        description=(
            "Show an agent's room: identity, status, recent activity,\n"
            "task board, and app notifications.\n\n"
            "Examples:\n"
            "  nutshell visit                       # latest session\n"
            "  nutshell visit 2026-03-25_11-06-53   # specific session\n"
            "  nutshell visit --json                # JSON output\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("session_id", nargs="?", default=None, metavar="SESSION_ID",
                   help="Session ID (default: latest session)")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="Output as JSON")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_visit)


def _cmd_visit(args) -> int:
    from ui.cli.visit import cmd_visit
    return cmd_visit(args)

# ── Subcommand: os ─────────────────────────────────────────────────────────

_CLI_OS_ENTITY = "cli_os"
_CLI_OS_MAX_AGE_HOURS = 24


def _find_recent_cli_os_session(
    sessions_base: Path,
    system_base: Path,
    max_age_hours: float = _CLI_OS_MAX_AGE_HOURS,
) -> str | None:
    """Find the most recent cli_os session created within *max_age_hours*.

    Returns the session ID or None.
    """
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    sessions = _read_all_sessions(sessions_base, system_base)
    for s in sessions:  # already sorted most-recent first
        if s.get("entity") != _CLI_OS_ENTITY:
            continue
        if s.get("status") == "stopped":
            continue
        created = s.get("created_at", "")
        if not created:
            continue
        try:
            ts = datetime.fromisoformat(created)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                return s["id"]
        except (ValueError, TypeError):
            continue
    return None


def _add_os_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "os",
        help="Launch a CLI-OS playground session.",
        description=(
            "Start an interactive CLI-OS session — a Linux-like playground\n"
            "where the agent is root and can freely code, explore, and build.\n\n"
            "If a cli_os session was created within the last 24 hours,\n"
            "it will be continued automatically.\n\n"
            "Examples:\n"
            "  nutshell os                         # open / resume CLI-OS\n"
            "  nutshell os 'build me a web server' # open with a task\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("message", nargs="?", default=None,
                   help="Optional message to send (default: greeting)")
    p.add_argument("--new", action="store_true", dest="force_new",
                   help="Force a new session (ignore recent ones)")
    p.add_argument("--timeout", type=float, default=300.0,
                   help="Seconds to wait for a response (default: 300)")
    p.add_argument("--no-wait", action="store_true",
                   help="Fire-and-forget (don't wait for response)")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_os)


def cmd_os(args) -> int:
    """Launch or continue a CLI-OS playground session."""
    message = args.message or "Hey! I just logged in. What's on this machine?"

    # Try to find a recent session to continue
    if not args.force_new:
        recent_id = _find_recent_cli_os_session(
            args.sessions_base, args.system_base,
        )
        if recent_id is not None:
            print(f"Resuming CLI-OS session: {recent_id}")
            from ui.cli.chat import _continue_session
            return _continue_session(
                recent_id, message,
                no_wait=args.no_wait,
                timeout=args.timeout,
                system_base=args.system_base,
            )

    # Create a new cli_os session
    from nutshell.runtime.session_factory import init_session
    entity_dir = _REPO_ROOT / "entity" / _CLI_OS_ENTITY
    if not entity_dir.exists():
        print(f"Error: entity '{_CLI_OS_ENTITY}' not found in entity/",
              file=sys.stderr)
        return 1

    session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    try:
        init_session(
            session_id=session_id,
            entity_name=_CLI_OS_ENTITY,
            sessions_base=args.sessions_base,
            system_sessions_base=args.system_base,
            initial_message=message,
        )
    except Exception as exc:
        print(f"Error creating CLI-OS session: {exc}", file=sys.stderr)
        return 1

    print(f"CLI-OS session: {session_id}")
    return 0


# ── Subcommand: meta ──────────────────────────────────────────────────────────

def _read_meta_info(meta_dir: Path) -> dict:
    memory_path = meta_dir / "core" / "memory.md"
    memory_dir = meta_dir / "core" / "memory"
    playground_dir = meta_dir / "playground"
    return {
        "entity": meta_dir.name,
        "path": str(meta_dir),
        "memory_exists": memory_path.exists(),
        "memory_bytes": memory_path.stat().st_size if memory_path.exists() else 0,
        "memory_layers": sorted([p.stem for p in memory_dir.glob("*.md")]) if memory_dir.is_dir() else [],
        "playground_files": sorted([str(p.relative_to(playground_dir)) for p in playground_dir.rglob("*") if p.is_file()]) if playground_dir.is_dir() else [],
        "params_exists": (meta_dir / "core" / "params.json").exists(),
    }


def _add_meta_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "meta",
        help="Show entity meta-session info.",
        description="Show all or one _meta session state.",
    )
    p.add_argument("entity", nargs="?", default=None, help="Entity name (optional)")
    p.add_argument("--memory", action="store_true", help="Print meta memory.md content")
    p.add_argument("--json", action="store_true", dest="as_json", help="Output as JSON")
    p.add_argument("--check", action="store_true", help="Show alignment diff for a specific entity")
    p.add_argument("--sync", choices=["entity-wins", "meta-wins"], help="Resolve alignment conflict")
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_meta)


def cmd_meta(args) -> int:
    from nutshell.runtime.meta_session import (
        MetaAlignmentError,
        check_meta_alignment,
        compute_meta_diffs,
        sync_entity_to_meta,
        sync_meta_to_entity,
    )

    base = args.sessions_base
    if not base.exists():
        print("No meta sessions found.")
        return 0

    if args.check or args.sync:
        if not args.entity:
            print("Error: ENTITY is required for --check/--sync.", file=sys.stderr)
            return 2
        meta_dir = base / f"{args.entity}_meta"
        if not meta_dir.is_dir():
            print(f"No meta-session found for entity: {args.entity}", file=sys.stderr)
            return 1
        if args.sync == "entity-wins":
            sync_entity_to_meta(args.entity, s_base=base)
            print(f"Synced entity → meta for {args.entity}")
            return 0
        if args.sync == "meta-wins":
            sync_meta_to_entity(args.entity, s_base=base)
            print(f"Synced meta → entity for {args.entity}")
            return 0
        diffs = compute_meta_diffs(args.entity, s_base=base)
        if not diffs:
            print(f"Meta-session aligned: {args.entity}")
            return 0
        print(MetaAlignmentError(args.entity, diffs).format_report())
        return 1

    if args.entity:
        meta_dir = base / f"{args.entity}_meta"
        if not meta_dir.is_dir():
            print(f"No meta-session found for entity: {args.entity}", file=sys.stderr)
            return 1
        targets = [meta_dir]
    else:
        targets = [p for p in sorted(base.iterdir()) if p.is_dir() and p.name.endswith("_meta")]

    if args.memory:
        if len(targets) != 1:
            print("Error: --memory requires a specific ENTITY.", file=sys.stderr)
            return 2
        memory_path = targets[0] / "core" / "memory.md"
        if memory_path.exists():
            print(memory_path.read_text(encoding="utf-8"), end="")
        return 0

    infos = [_read_meta_info(p) for p in targets]
    if args.as_json:
        print(json.dumps(infos if not args.entity else infos[0], ensure_ascii=False, indent=2))
        return 0

    for idx, info in enumerate(infos):
        print(f"ENTITY: {info['entity']}")
        print(f"PATH: {info['path']}")
        print(f"MEMORY: {info['memory_bytes']} bytes")
        print(f"LAYERS: {', '.join(info['memory_layers']) if info['memory_layers'] else '—'}")
        print(f"PLAYGROUND FILES: {len(info['playground_files'])}")
        print(f"PARAMS: {'yes' if info['params_exists'] else 'no'}")
        if idx != len(infos) - 1:
            print()
    return 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nutshell",
        description="Nutshell agent runtime CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Session management (no server required):\n"
            "  nutshell sessions                   List all sessions\n"
            "  nutshell new [ID] [--entity NAME]   Create a session\n"
            "  nutshell chat MESSAGE               New session + send message\n"
            "  nutshell chat --session ID MSG      Send to existing session\n"
            "  nutshell stop SESSION_ID            Stop heartbeat\n"
            "  nutshell start SESSION_ID           Resume heartbeat\n"
            "  nutshell log [SESSION_ID] [-n N]    Show conversation history\n"
            "  nutshell tasks [SESSION_ID]         Show session task board\n"
            "  nutshell friends [--json]           IM-style contact list\n  nutshell kanban                     Unified task board (all sessions)\n  nutshell kanban --session ID        Single session task board\n\n"
            "Entity management:\n"
            "  nutshell entity new                 Scaffold entity interactively\n"
            "  nutshell entity new -n NAME         Scaffold entity by name\n"
            "  nutshell entity log NAME            Show entity version changelog\n\n"
            "Diagnostics:\n"
            "  nutshell prompt-stats [SESSION_ID]  Show prompt space breakdown\n"
            "  nutshell token-report [SESSION_ID]  Show per-turn token costs\n\n"
            "Repo skills:\n"
            "  nutshell repo-skill PATH            Generate codebase overview SKILL.md\n"
            "  nutshell repo-skill PATH -n NAME     Custom skill name\n"
            "  nutshell repo-dev PATH               Create dev agent for repo\n"
            "  nutshell repo-dev PATH -m MSG         … with initial task\n\n"
            "Playground:\n"
            "  nutshell os [MESSAGE]               CLI-OS playground session\n\n"
            "Other:\n"
            "  nutshell review                     Review agent update requests\n"
            "  nutshell server                     Start the server\n"
            "  nutshell web                        Start the web UI (monitoring)\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    _add_chat_parser(subparsers)
    _add_sessions_parser(subparsers)
    _add_new_parser(subparsers)
    _add_stop_parser(subparsers)
    _add_start_parser(subparsers)
    _add_log_parser(subparsers)
    _add_tasks_parser(subparsers)
    _add_entity_parser(subparsers)
    _add_prompt_stats_parser(subparsers)
    _add_token_report_parser(subparsers)
    _add_review_parser(subparsers)
    _add_friends_parser(subparsers)
    _add_kanban_parser(subparsers)
    _add_repo_skill_parser(subparsers)
    _add_repo_dev_parser(subparsers)
    _add_visit_parser(subparsers)
    _add_os_parser(subparsers)
    _add_meta_parser(subparsers)
    _add_exec_parser(subparsers, "server", "Start the Nutshell server daemon.")
    _add_exec_parser(subparsers, "web",    "Start the web UI at http://localhost:8080 (monitoring).")

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
