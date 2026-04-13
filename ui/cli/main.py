"""nutshell — unified CLI for the Nutshell agent runtime.

Usage:
    nutshell chat MESSAGE [options]          Send a message / create a session
    nutshell sessions [--json]              List all sessions
    nutshell new [SESSION_ID] [options]     Create a new session (no message)
    nutshell stop SESSION_ID                Stop a session
    nutshell start SESSION_ID               Resume a stopped session
    nutshell log [SESSION_ID] [-n N] [--since T] [--watch]  Show conversation history
    nutshell tasks [SESSION_ID]             Show a session's task board
    nutshell entity new [options]           Scaffold a new entity directory

    nutshell prompt-stats [SESSION_ID]      Show prompt space breakdown for a session
    nutshell repo-skill REPO_PATH           Generate codebase overview skill

    nutshell server                         Start the Nutshell server (auto-daemonize)
    nutshell web                            Start the web UI (monitoring)
    nutshell dream ENTITY                    Trigger meta session dream cycle

All session-management commands (sessions, new, stop, start, tasks) work without
a running server — they read/write the _sessions/ directory directly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_SESSIONS_BASE = _REPO_ROOT / "sessions"
_DEFAULT_SYSTEM_BASE = _REPO_ROOT / "_sessions"


# ── Server auto-start ─────────────────────────────────────────────────────────

def _ensure_server_running(
    sessions_dir: Path | None = None,
    system_sessions_dir: Path | None = None,
) -> None:
    """Start nutshell-server in daemon mode if not already running."""
    from nutshell.runtime.server import _is_server_running, _start_daemon
    sys_dir = system_sessions_dir or _DEFAULT_SYSTEM_BASE
    if _is_server_running(sys_dir):
        return
    print("Starting nutshell server...")
    _start_daemon(
        sessions_dir=sessions_dir or _DEFAULT_SESSIONS_BASE,
        system_sessions_dir=sys_dir,
    )


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
            secs = int((datetime.now() - dt).total_seconds())
        else:
            secs = int((datetime.now(tz=timezone.utc) - dt).total_seconds())
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
    *,
    exclude_meta: bool = False,
) -> list[dict]:
    """Read all sessions from _sessions/ + sessions/. No server required."""
    from nutshell.service import list_sessions
    return list_sessions(sessions_base, system_base, exclude_meta=exclude_meta)


# ── Subcommand: chat ──────────────────────────────────────────────────────────

def _add_chat_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "chat",
        allow_abbrev=False,
        help="Send a message to a session and print the response.",
        description=(
            "Send a message to an existing session or create a new one.\n\n"
            "Examples:\n"
            "  nutshell chat 'Plan a data pipeline'\n"
            "  nutshell chat --entity nutshell_dev 'Review this code'\n"
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
    _ensure_server_running(args.sessions_base, args.system_base)
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
        allow_abbrev=False,
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
        allow_abbrev=False,
        help="Create a new session (no message — use 'chat' to send immediately).",
        description=(
            "Create a session from an entity. Session ID is auto-generated from\n"
            "the current timestamp unless specified explicitly.\n\n"
            "Examples:\n"
            "  nutshell new\n"
            "  nutshell new --entity nutshell_dev\n"
            "  nutshell new my-project --entity agent\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("session_id", nargs="?", default=None,
                   help="Session ID (default: current timestamp)")
    p.add_argument("--entity", default="agent", metavar="NAME",
                   help="Entity to initialise from (default: agent)")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--inject-memory", action="append", metavar="KEY=VALUE",
                   dest="inject_memory",
                   help="Inject a memory layer: KEY=VALUE or KEY=@FILE (repeatable)")
    p.set_defaults(func=cmd_new)


def cmd_new(args) -> int:
    _ensure_server_running(args.sessions_base, args.system_base)
    from nutshell.service import create_session
    session_id = args.session_id or (datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "-" + uuid.uuid4().hex[:4])
    entity_dir = _REPO_ROOT / "entity" / args.entity
    if not entity_dir.exists():
        print(f"Error: entity '{args.entity}' not found in entity/", file=sys.stderr)
        return 1
    try:
        create_session(session_id, args.entity, sessions_dir=args.sessions_base, system_sessions_dir=args.system_base)
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
        help="Stop a session.",
    )
    p.add_argument("session_id", help="Session ID to stop")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_stop)


def cmd_stop(args) -> int:
    from nutshell.service import stop_session
    try:
        if not stop_session(args.session_id, args.system_base):
            print(f"Error: session '{args.session_id}' not found", file=sys.stderr)
            return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
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
    from nutshell.service import start_session
    if not start_session(args.session_id, args.system_base):
        print(f"Error: session '{args.session_id}' not found", file=sys.stderr)
        return 1
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
        allow_abbrev=False,
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
    p.add_argument("session_id", nargs="?", default=argparse.SUPPRESS,
                   help="Session ID (default: most recently active session)")
    p.add_argument("--session", dest="session_id", metavar="ID", default=None,
                   help="Session ID (alias for positional session_id)")
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
    from nutshell.service import get_log_turns, get_pending_inputs
    session_id = args.session_id

    if not session_id:
        sessions = _read_all_sessions(args.sessions_base, args.system_base, exclude_meta=True)
        if not sessions:
            print("No sessions found.", file=sys.stderr)
            return 1
        session_id = sessions[0]["id"]

    context_path = args.system_base / session_id / "context.jsonl"
    since_raw = getattr(args, "since", None)
    watch_mode = getattr(args, "watch", False)
    if watch_mode and since_raw is None:
        since_raw = "now"
    if watch_mode:
        return _watch_log(args, session_id, context_path, _parse_since(since_raw))

    try:
        turns_to_show = get_log_turns(session_id, args.system_base, n=None if since_raw else args.num_turns, since=since_raw)
        pending_inputs = get_pending_inputs(session_id, args.system_base, n=args.num_turns) if not turns_to_show and since_raw is None else []
    except FileNotFoundError:
        print(f"Error: session '{session_id}' not found", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not turns_to_show:
        if pending_inputs:
            print(f"[{session_id}] — pending (no agent response yet)")
            print("─" * 60)
            for row in pending_inputs:
                print(f"  USER  {row['ts']}  {row['user']}")
        elif not context_path.exists():
            print(f"[{session_id}] No conversation history yet.")
        elif since_raw is not None:
            print(f"[{session_id}] No new turns since {since_raw}.")
        else:
            print(f"[{session_id}] No conversation history yet.")
        return 0

    print(f"[{session_id}] {len(turns_to_show)} turn(s)" + (f" since {since_raw}" if since_raw else ""))
    print("─" * 60)
    for row in turns_to_show:
        if row["user"]:
            print(f"  USER  {row['ts']}  {row['user']}")
        for line in row["agent"]:
            print(f"  AGENT          {line}")
        usage = row["usage"]
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


# ── Subcommand: tasks ─────────────────────────────────────────────────────────

def _add_tasks_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "tasks",
        allow_abbrev=False,
        help="Show a session's task cards (core/tasks/).",
        description=(
            "Display task cards for a session.\n\n"
            "Examples:\n"
            "  nutshell tasks                       Show latest session's tasks\n"
            "  nutshell tasks 2026-03-25_10-00-00   Show specific session\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("session_id", nargs="?", default=argparse.SUPPRESS,
                   help="Session ID (default: most recently active session)")
    p.add_argument("--session", dest="session_id", metavar="ID", default=None,
                   help="Session ID (alias for positional session_id)")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_tasks)


def cmd_tasks(args) -> int:
    from nutshell.service import get_tasks

    session_id = args.session_id
    if not session_id:
        sessions = _read_all_sessions(args.sessions_base, args.system_base, exclude_meta=True)
        if not sessions:
            print("No sessions found.", file=sys.stderr)
            return 1
        session_id = sessions[0]["id"]

    tasks_dir = args.sessions_base / session_id / "core" / "tasks"
    if not (args.system_base / session_id / "manifest.json").exists():
        print(f"Error: session '{session_id}' not found", file=sys.stderr)
        return 1

    cards = get_tasks(session_id, args.sessions_base)
    print(f"[{session_id}] task cards ({len(cards)})")
    print("─" * 60)
    if not cards:
        print("(empty)")
    else:
        for card in cards:
            interval_str = f"every {card['interval']}s" if card['interval'] else "one-shot"
            print(f"  [{card['status']}] {card['name']}  ({interval_str})")
            if card.get('last_finished_at'):
                print(f"          last finished: {card['last_finished_at']}")
            desc = card.get('description') or ''
            for line in desc.splitlines()[:3]:
                print(f"          {line}")
            if len(desc.splitlines()) > 3:
                print(f"          ...")
    return 0


# ── Subcommand: prompt-stats ──────────────────────────────────────────────────

_MEMORY_LAYER_INLINE_LINES = 60  # must match Agent._MEMORY_LAYER_INLINE_LINES


def _add_prompt_stats_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "prompt-stats",
        allow_abbrev=False,
        help="Show prompt space breakdown for a session.",
        description=(
            "Display a component-by-component breakdown of system prompt size.\n\n"
            "Examples:\n"
            "  nutshell prompt-stats                       Latest session\n"
            "  nutshell prompt-stats 2026-03-25_10-00-00   Specific session\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("session_id", nargs="?", default=argparse.SUPPRESS,
                   help="Session ID (default: most recently active session)")
    p.add_argument("--session", dest="session_id", metavar="ID", default=None,
                   help="Session ID (alias for positional session_id)")
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
    from nutshell.service import get_prompt_stats
    session_id = args.session_id
    if not session_id:
        sessions = _read_all_sessions(args.sessions_base, args.system_base, exclude_meta=True)
        if not sessions:
            print("No sessions found.", file=sys.stderr)
            return 1
        session_id = sessions[0]["id"]

    try:
        stats = get_prompt_stats(session_id, args.sessions_base, args.system_base)
    except FileNotFoundError:
        print(f"Error: session '{session_id}' not found", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    rows = [(r["label"], r["lines"], r["chars"], r["tokens"], r["note"]) for r in stats["rows"]]

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

    # Group: Task
    print("  TASK")
    label, lines, chars, tokens, note = rows[-1]
    print(f"  {label:<{COL[0]}}  {lines:>{COL[1]}}  {chars:>{COL[2]}}  {tokens:>{COL[3]}}  {note}")

    print(sep)

    # Totals (static + dynamic, excluding task prompt)
    chat_rows = rows[:-1]
    total_chars = sum(r[2] for r in chat_rows)
    total_tokens = sum(r[3] for r in chat_rows)
    static_chars = sum(r[2] for r in static_rows)
    static_tokens = sum(r[3] for r in static_rows)
    dynamic_chars = sum(r[2] for r in dynamic_rows)
    dynamic_tokens = sum(r[3] for r in dynamic_rows)
    print(f"  {'TOTAL (chat)':<{COL[0]}}  {'':>{COL[1]}}  {total_chars:>{COL[2]}}  {total_tokens:>{COL[3]}}  static {static_tokens} + dynamic {dynamic_tokens}")
    print()
    print("  * task.md is injected during autonomous task ticks, not regular chat.")
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
            "  nutshell entity new -n my-agent --init-from agent\n"
            "  nutshell entity new -n my-agent --blank\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    enew.add_argument("-n", "--name", metavar="NAME", help="Entity name")
    enew.add_argument("--init-from", metavar="SOURCE",
                      help="Copy all files from this entity (skips picker)")
    enew.add_argument("--blank", action="store_true",
                      help="Create a blank entity with empty files")
    enew.add_argument("--entity-dir", default="entity", metavar="DIR",
                      help="Base directory for entities (default: entity/)")

    p.set_defaults(func=cmd_entity)


def cmd_entity(args) -> int:
    if args.entity_cmd == "new":
        from ui.cli.new_agent import _ask_name, _ask_init_from, create_entity
        entity_dir = Path(args.entity_dir)
        name = args.name or _ask_name()
        init_from_arg = getattr(args, "init_from", None)
        if args.blank:
            init_from = None
        elif init_from_arg:
            init_from = init_from_arg
        elif args.name:
            # Non-interactive: -n NAME given but no --init-from/--blank → default to agent
            init_from = "agent"
        else:
            init_from = _ask_init_from(entity_dir)
        try:
            created = create_entity(name, entity_dir, init_from)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Created: {created}/")
        if init_from:
            print(f"  (initialized from '{init_from}')")
        return 0

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


# ── Subcommand: dream ─────────────────────────────────────────────────────────

def _add_dream_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "dream",
        allow_abbrev=False,
        help="Trigger the meta agent to run its dream cycle.",
        description="Sends a wake-up message to the entity's meta session, prompting it to review all child sessions.",
    )
    p.add_argument("entity", help="Entity name")
    p.add_argument("--message", default="看任务来执行", help="Message to send (default: '看任务来执行')")
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE, help=argparse.SUPPRESS)
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_dream)


def cmd_dream(args) -> int:
    """Send a wake-up message to the entity's meta session to trigger the dream cycle."""
    from nutshell.session_engine.entity_state import get_meta_session_id
    from nutshell.service import send_message

    meta_id = get_meta_session_id(args.entity)
    sys_dir = args.system_base / meta_id

    if not sys_dir.exists():
        print(f"Meta session for '{args.entity}' not found at {sys_dir}")
        print(f"Hint: create a session for entity '{args.entity}' to initialise its meta session.")
        return 1

    msg_id = send_message(meta_id, args.message, args.system_base)
    print(f"Sent to {meta_id}: '{args.message}' (id={msg_id})")
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
        "config_exists": (meta_dir / "core" / "config.yaml").exists(),
    }


def _add_meta_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "meta",
        allow_abbrev=False,
        help="Show entity meta-session info.",
        description="Show all or one _meta session state.",
    )
    p.add_argument("entity", nargs="?", default=None, help="Entity name (optional)")
    p.add_argument("--memory", action="store_true", help="Print meta memory.md content")
    p.add_argument("--versions", action="store_true", help="Show version history for a specific entity")
    p.add_argument("--json", action="store_true", dest="as_json", help="Output as JSON")
    p.add_argument("--init", action="store_true", help="Re-run gene commands (delete marker and re-execute)")
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_meta)


def cmd_meta(args) -> int:
    from nutshell.session_engine.entity_state import (
        get_meta_version,
        get_version_history,
        run_gene_commands,
    )

    base = args.sessions_base
    if not base.exists():
        print("No meta sessions found.")
        return 0

    if args.init:
        if not args.entity:
            print("Error: ENTITY is required for --init.", file=sys.stderr)
            return 2
        meta_dir = base / f"{args.entity}_meta"
        marker = meta_dir / "core" / ".gene_initialized"
        if marker.exists():
            marker.unlink()
            print(f"Removed gene marker for {args.entity}")
        run_gene_commands(args.entity, s_base=base)
        return 0

    if args.versions:
        if not args.entity:
            print("Error: ENTITY is required for --versions.", file=sys.stderr)
            return 2
        # Derive _sessions/ sibling from the sessions_base argument
        sys_base = args.sessions_base.parent / "_sessions"
        history = get_version_history(args.entity, sys_base=sys_base)
        current = get_meta_version(args.entity, sys_base=sys_base)
        if args.as_json:
            print(json.dumps({"current": current, "history": history}, ensure_ascii=False, indent=2))
            return 0
        print(f"Entity: {args.entity}  current version: {current or '(none)'}")
        if not history:
            print("No version history.")
        else:
            for entry in history:
                note = f"  {entry['note']}" if entry.get("note") else ""
                print(f"  {entry['version']}  {entry.get('ts', '')}  {note}")
        return 0

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
        print(f"PARAMS: {'yes' if info['config_exists'] else 'no'}")
        if idx != len(infos) - 1:
            print()
    return 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nutshell",
        description="Nutshell agent runtime CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
        epilog=(
            "Session management (no server required):\n"
            "  nutshell sessions                   List all sessions\n"
            "  nutshell new [ID] [--entity NAME]   Create a session\n"
            "  nutshell chat MESSAGE               New session + send message\n"
            "  nutshell chat --session ID MSG      Send to existing session\n"
            "  nutshell stop SESSION_ID            Stop a session\n"
            "  nutshell start SESSION_ID           Resume a session\n"
            "  nutshell log [SESSION_ID] [-n N]    Show conversation history\n"
            "  nutshell tasks [SESSION_ID]         Show session task board\n\n"
            "Entity management:\n"
            "  nutshell entity new                 Scaffold entity interactively\n"
            "  nutshell entity new -n NAME         Scaffold entity by name\n\n"
            "Diagnostics:\n"
            "  nutshell prompt-stats [SESSION_ID]  Show prompt space breakdown\n\n"
            "Repo skills:\n"
            "  nutshell repo-skill PATH            Generate codebase overview SKILL.md\n"
            "  nutshell repo-skill PATH -n NAME     Custom skill name\n"
            "  nutshell repo-dev PATH               Create dev agent for repo\n"
            "  nutshell repo-dev PATH -m MSG         … with initial task\n\n"
            "Dream (session cleanup):\n"
            "  nutshell dream ENTITY                 Trigger meta session dream cycle\n"
            "\n"
            "Other:\n"
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

    _add_repo_skill_parser(subparsers)
    _add_repo_dev_parser(subparsers)
    _add_dream_parser(subparsers)
    _add_meta_parser(subparsers)
    _add_exec_parser(subparsers, "server", "Start the Nutshell server daemon.")
    _add_exec_parser(subparsers, "web",    "Start the web UI at http://localhost:8080 (monitoring).")

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
