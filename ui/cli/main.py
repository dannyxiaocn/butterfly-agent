"""nutshell — unified CLI for the Nutshell agent runtime.

Usage:
    nutshell chat MESSAGE [options]          Send a message / create a session
    nutshell sessions [--json]              List all sessions
    nutshell new [SESSION_ID] [options]     Create a new session (no message)
    nutshell stop SESSION_ID                Stop a session's heartbeat
    nutshell start SESSION_ID               Resume a stopped session
    nutshell entity new [options]           Scaffold a new entity directory
    nutshell review                         Review pending entity update requests
    nutshell server                         Start the Nutshell server
    nutshell web                            Start the web UI
    nutshell tui                            Start the terminal UI

All session-management commands (sessions, new, stop, start) work without
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
    p.add_argument("--timeout", type=float, default=120.0,
                   help="Seconds to wait for a response (default: 120)")
    p.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                   help=argparse.SUPPRESS)
    p.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_chat)


def cmd_chat(args) -> int:
    from ui.cli.chat import _continue_session, _new_session
    if args.session:
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


# ── Subcommands: server / web / tui ──────────────────────────────────────────

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
        "tui":    ("ui.tui", "main"),
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
            "  nutshell start SESSION_ID           Resume heartbeat\n\n"
            "Entity management:\n"
            "  nutshell entity new                 Scaffold entity interactively\n"
            "  nutshell entity new -n NAME         Scaffold entity by name\n\n"
            "Other:\n"
            "  nutshell review                     Review agent update requests\n"
            "  nutshell server                     Start the server\n"
            "  nutshell web                        Start the web UI\n"
            "  nutshell tui                        Start the terminal UI\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    _add_chat_parser(subparsers)
    _add_sessions_parser(subparsers)
    _add_new_parser(subparsers)
    _add_stop_parser(subparsers)
    _add_start_parser(subparsers)
    _add_entity_parser(subparsers)
    _add_review_parser(subparsers)
    _add_exec_parser(subparsers, "server", "Start the Nutshell server daemon.")
    _add_exec_parser(subparsers, "web",    "Start the web UI at http://localhost:8080.")
    _add_exec_parser(subparsers, "tui",    "Start the terminal UI.")

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
