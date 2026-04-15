"""butterfly — unified CLI for the Butterfly agent runtime.

Usage:
    butterfly chat MESSAGE [options]          Send a message / create a session
    butterfly sessions [--json]               List all sessions
    butterfly new [SESSION_ID] [options]      Create a new session (no message)
    butterfly stop SESSION_ID                 Stop a session
    butterfly start SESSION_ID                Resume a stopped session
    butterfly log [SESSION_ID] [-n N] [--since T] [--watch]  Show conversation history
    butterfly tasks [SESSION_ID]              Show a session's task board
    butterfly panel [SESSION_ID] [options]    Show a session's panel (pending tools)
    butterfly entity new [options]            Scaffold a new entity directory

    butterfly server                          Start the Butterfly server (auto-daemonize)
    butterfly web                             Start the web UI (monitoring)

All session-management commands (sessions, new, stop, start, tasks) work without
a running server — they read/write the _sessions/ directory directly.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
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
    """Start butterfly-server in daemon mode if not already running."""
    from butterfly.runtime.server import _is_server_running, _start_daemon
    sys_dir = system_sessions_dir or _DEFAULT_SYSTEM_BASE
    if _is_server_running(sys_dir):
        return
    print("Starting butterfly server...")
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
    from butterfly.service import list_sessions
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
            "  butterfly chat 'Plan a data pipeline'\n"
            "  butterfly chat --entity butterfly_dev 'Review this code'\n"
            "  butterfly chat --session 2026-03-25_10-00-00 'Status?'\n"
            "  butterfly chat --session <id> --no-wait 'Run overnight'\n"
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
            "  butterfly new\n"
            "  butterfly new --entity butterfly_dev\n"
            "  butterfly new my-project --entity agent\n"
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
    from butterfly.service import create_session
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
    from butterfly.service import stop_session
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
    from butterfly.service import start_session
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
            "  butterfly log                                  Show latest session, last 5 turns\n"
            "  butterfly log 2026-03-25_10-00-00              Specific session\n"
            "  butterfly log -n 20                            Last 20 turns\n"
            "  butterfly log --since now                      Bookmark 'now', future calls show new turns only\n"
            "  butterfly log --since 2026-03-25T12:00:00      Turns after a specific time\n"
            "  butterfly log --watch                          Poll every 2s for new turns (Ctrl-C to stop)\n"
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
    from butterfly.service import get_log_turns, get_pending_inputs
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
            "  butterfly tasks                       Show latest session's tasks\n"
            "  butterfly tasks 2026-03-25_10-00-00   Show specific session\n"
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
    from butterfly.service import get_tasks

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


# ── Subcommand: panel ─────────────────────────────────────────────────────────

def _add_panel_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "panel",
        allow_abbrev=False,
        help="Show a session's panel entries (core/panel/).",
        description=(
            "Display panel entries for a session — the in-loop work surface\n"
            "that tracks non-blocking tool calls (and, later, sub-agent refs).\n\n"
            "Examples:\n"
            "  butterfly panel                              Latest session's panel\n"
            "  butterfly panel 2026-03-25_10-00-00          Specific session\n"
            "  butterfly panel <ID> --tid bg_abc            Full entry detail\n"
            "  butterfly panel <ID> --tid bg_abc --output   Dump output_file\n"
            "  butterfly panel <ID> --tid bg_abc --kill     Mark entry killed\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("session_id", nargs="?", default=argparse.SUPPRESS,
                   help="Session ID (default: most recently active session)")
    p.add_argument("--session", dest="session_id", metavar="ID", default=None,
                   help="Session ID (alias for positional session_id)")
    p.add_argument("--tid", metavar="TID", default=None,
                   help="Show detail for a single panel entry")
    p.add_argument("--kill", action="store_true", default=False,
                   help="With --tid: mark the entry killed (file-level only)")
    p.add_argument("--output", action="store_true", default=False,
                   help="With --tid: print the full output_file contents")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_panel)


def _resolve_output_file(raw: str | None) -> Path | None:
    """Resolve PanelEntry.output_file (often relative to repo root)."""
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return path


def _last_nonempty_line(path: Path, max_chars: int = 80) -> str:
    """Return the last non-empty line of a file (truncated), or '' on any failure."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            last = ""
            for line in fh:
                stripped = line.rstrip("\n")
                if stripped.strip():
                    last = stripped
        if len(last) > max_chars:
            return last[: max_chars - 1] + "…"
        return last
    except OSError:
        return ""


def cmd_panel(args) -> int:
    from butterfly.session_engine.panel import (
        STATUS_KILLED,
        TERMINAL_STATUSES,
        list_entries,
        load_entry,
        save_entry,
    )

    session_id = args.session_id
    if not session_id:
        sessions = _read_all_sessions(args.sessions_base, args.system_base, exclude_meta=True)
        if not sessions:
            print("No sessions found.", file=sys.stderr)
            return 1
        session_id = sessions[0]["id"]

    if not (args.system_base / session_id / "manifest.json").exists():
        print(f"Error: session '{session_id}' not found.", file=sys.stderr)
        return 2

    panel_dir = args.sessions_base / session_id / "core" / "panel"

    # ── Detail / kill / output mode ─────────────────────────────────────
    if args.tid:
        entry = load_entry(panel_dir, args.tid)
        if entry is None:
            print(
                f"Error: no panel entry with tid '{args.tid}' in session '{session_id}'.",
                file=sys.stderr,
            )
            return 2

        output_path = _resolve_output_file(entry.output_file)

        # --output: raw dump, streamed so huge output files don't explode memory.
        if args.output:
            if output_path is None or not output_path.exists():
                print(f"No output file for {entry.tid}.")
                return 0
            try:
                sys.stdout.flush()
                with output_path.open("rb") as fh:
                    shutil.copyfileobj(fh, sys.stdout.buffer)
                sys.stdout.buffer.flush()
            except OSError as exc:
                print(f"Error: failed to read {output_path}: {exc}", file=sys.stderr)
                return 1
            return 0

        # --kill: mark entry killed on disk only
        if args.kill:
            if entry.status in TERMINAL_STATUSES:
                print(f"{entry.tid} already terminal (status={entry.status}); no change.")
                return 0
            entry.status = STATUS_KILLED
            entry.finished_at = time.time()
            save_entry(panel_dir, entry)
            print(
                f"Marked {entry.tid} killed. If the process is still alive, "
                f"it may continue until BackgroundTaskManager reaps it on next daemon tick."
            )
            return 0

        # Default --tid: pretty JSON + last 40 lines of output + footer.
        # Use a bounded deque so gigabyte output files don't materialise in
        # RAM just to extract a 40-line tail.
        print(json.dumps(entry.to_json(), indent=2, default=str, ensure_ascii=False))
        if output_path is not None and output_path.exists():
            tail: collections.deque[str] | None = None
            try:
                tail = collections.deque(maxlen=40)
                with output_path.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        tail.append(line.rstrip("\n"))
            except OSError as exc:
                print(f"\n[output_file read error: {exc}]")
                tail = None
            if tail is not None:
                print()
                print(f"── last {len(tail)} line(s) of {output_path} ──")
                for line in tail:
                    print(line)
            bytes_n = output_path.stat().st_size if output_path.exists() else 0
            print(f"\n[bytes={bytes_n} output_file={output_path}]")
        else:
            print(f"\n[bytes=0 output_file={entry.output_file or '-'}]")
        return 0

    # ── List mode ───────────────────────────────────────────────────────
    entries = list_entries(panel_dir)
    print(f"[{session_id}] panel entries ({len(entries)})")
    print("─" * 60)
    if not entries:
        print("(empty)")
        return 0

    # Columns: tid (12) tool_name (20) status (18) tail
    COL = {"tid": 12, "tool": 20, "status": 18}
    for e in entries:
        output_path = _resolve_output_file(e.output_file)
        tail = _last_nonempty_line(output_path) if output_path and output_path.exists() else ""
        print(
            f"  {e.tid:<{COL['tid']}}  {e.tool_name:<{COL['tool']}}  "
            f"[{e.status}]".ljust(COL['status'] + 2)
            + f"  {tail}"
        )
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
            "  butterfly entity new                          # interactive\n"
            "  butterfly entity new -n my-agent\n"
            "  butterfly entity new -n my-agent --init-from agent\n"
            "  butterfly entity new -n my-agent --blank\n"
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
    cmd = f"butterfly-{name}"
    path = shutil.which(cmd)
    if path:
        os.execv(path, [path])
    # Fallback: call the Python module directly
    mapping = {
        "server": ("butterfly.runtime.server", "main"),
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



# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="butterfly",
        description="Butterfly agent runtime CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
        epilog=(
            "Session management (no server required):\n"
            "  butterfly sessions                   List all sessions\n"
            "  butterfly new [ID] [--entity NAME]   Create a session\n"
            "  butterfly chat MESSAGE               New session + send message\n"
            "  butterfly chat --session ID MSG      Send to existing session\n"
            "  butterfly stop SESSION_ID            Stop a session\n"
            "  butterfly start SESSION_ID           Resume a session\n"
            "  butterfly log [SESSION_ID] [-n N]    Show conversation history\n"
            "  butterfly tasks [SESSION_ID]         Show session task board\n"
            "  butterfly panel [SESSION_ID]         Show session panel entries\n\n"
            "Entity management:\n"
            "  butterfly entity new                 Scaffold entity interactively\n"
            "  butterfly entity new -n NAME         Scaffold entity by name\n\n"
            "Provider login:\n"
            "  butterfly codex login                OAuth flow for ChatGPT Codex\n"
            "  butterfly kimi login                 Paste Kimi API key into .env\n\n"
            "Other:\n"
            "  butterfly server                     Start the server\n"
            "  butterfly web                        Start the web UI (monitoring)\n"
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
    _add_panel_parser(subparsers)
    _add_entity_parser(subparsers)
    from ui.cli.login import _add_codex_parser, _add_kimi_parser
    _add_codex_parser(subparsers)
    _add_kimi_parser(subparsers)
    _add_exec_parser(subparsers, "server", "Start the Butterfly server daemon.")
    _add_exec_parser(subparsers, "web",    "Start the web UI at http://localhost:7720 (monitoring).")

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
