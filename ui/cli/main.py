"""nutshell — unified CLI for the Nutshell agent runtime.

Usage:
    nutshell chat MESSAGE [options]          Send a message / create a session
    nutshell sessions [--json]              List all sessions
    nutshell new [SESSION_ID] [options]     Create a new session (no message)
    nutshell stop SESSION_ID                Stop a session's heartbeat
    nutshell start SESSION_ID               Resume a stopped session
    nutshell log [SESSION_ID] [-n N]        Show recent conversation history
    nutshell tasks [SESSION_ID]             Show a session's task board
    nutshell entity new [options]           Scaffold a new entity directory
    nutshell review                         Review pending entity update requests
    nutshell server                         Start the Nutshell server
    nutshell web                            Start the web UI (monitoring)

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

def _add_log_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "log",
        help="Show recent conversation history for a session.",
        description=(
            "Display the last N conversation turns from a session.\n\n"
            "Examples:\n"
            "  nutshell log                         Show latest session, last 5 turns\n"
            "  nutshell log 2026-03-25_10-00-00     Specific session, last 5 turns\n"
            "  nutshell log --n 20                  Latest session, last 20 turns\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("session_id", nargs="?", default=None,
                   help="Session ID (default: most recently active session)")
    p.add_argument("-n", type=int, default=5, dest="num_turns",
                   metavar="N", help="Number of turns to show (default: 5)")
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

    lines = [l for l in context_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    # Group into (user_input, turn) pairs
    inputs_by_id: dict[str, dict] = {}
    turns: list[dict] = []
    for ev in events:
        if ev.get("type") == "user_input":
            inputs_by_id[ev["id"]] = ev
        elif ev.get("type") == "turn":
            turns.append(ev)

    # Show last N turns
    turns_to_show = turns[-args.num_turns:]
    if not turns_to_show:
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

    print(f"[{session_id}] last {len(turns_to_show)} turn(s)")
    print("─" * 60)
    for turn in turns_to_show:
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
            "  nutshell tasks [SESSION_ID]         Show session task board\n\n"
            "Entity management:\n"
            "  nutshell entity new                 Scaffold entity interactively\n"
            "  nutshell entity new -n NAME         Scaffold entity by name\n\n"
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
    _add_review_parser(subparsers)
    _add_exec_parser(subparsers, "server", "Start the Nutshell server daemon.")
    _add_exec_parser(subparsers, "web",    "Start the web UI at http://localhost:8080 (monitoring).")

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
