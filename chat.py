#!/usr/bin/env python3
"""Nutshell chat UI — connects to a running nutshell server.

Usage:
    python chat.py                                      # Create new instance (random ID)
    python chat.py --create my-project                 # Create named instance
    python chat.py --create my-project --entity entity/agent_core
    python chat.py --attach my-project                 # Attach to existing instance
    python chat.py --list                              # List all instances

Commands during chat:
    /instances    List all instances
    /kanban       Show current instance kanban
    /status       Show server status for this instance
    /exit         Exit chat (server + instance keep running)
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.patch_stdout import patch_stdout

INSTANCES_DIR = Path("instances")
_REPO_ROOT = Path(__file__).parent
_DEFAULT_ENTITY = "entity/agent_core"


# ── CLI ────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Nutshell chat UI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--create", metavar="ID", nargs="?", const="",
        help="Create a new instance (optionally with a specific ID)",
    )
    group.add_argument("--attach", metavar="ID", help="Attach to an existing instance")
    group.add_argument("--list", "-l", action="store_true", help="List all instances")

    parser.add_argument(
        "--entity", "-e", default=_DEFAULT_ENTITY, metavar="DIR",
        help=f"Entity directory for the new instance (default: {_DEFAULT_ENTITY})",
    )
    parser.add_argument(
        "--heartbeat", default=10.0, type=float, metavar="SECONDS",
        help="Heartbeat interval in seconds (default: 10)",
    )
    parser.add_argument(
        "--instances-dir", default=str(INSTANCES_DIR), metavar="DIR",
        help=f"Instances directory (default: {INSTANCES_DIR})",
    )
    return parser.parse_args()


# ── Instance management ────────────────────────────────────────────────────

def _list_instances(instances_dir: Path) -> None:
    if not instances_dir.exists() or not any(instances_dir.iterdir()):
        print("No instances found.")
        return

    rows = []
    for d in sorted(instances_dir.iterdir()):
        if not d.is_dir():
            continue
        manifest_path = d / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
        entity = manifest.get("entity", "unknown")
        pid_path = d / "daemon.pid"
        status = "running" if pid_path.exists() else manifest.get("status", "idle")
        rows.append((d.name, entity, status))

    if not rows:
        print("No instances found.")
        return

    print(f"{'ID':<32} {'Entity':<26} Status")
    print("─" * 70)
    for iid, entity, status in rows:
        print(f"{iid:<32} {entity:<26} {status}")


def _create_instance(
    instances_dir: Path, instance_id: str, entity: str, heartbeat: float
) -> Path:
    instance_dir = instances_dir / instance_id
    instance_dir.mkdir(parents=True, exist_ok=True)
    (instance_dir / "files").mkdir(exist_ok=True)

    manifest = {
        "instance_id": instance_id,
        "entity": entity,
        "created_at": datetime.now().isoformat(),
        "heartbeat": heartbeat,
    }
    (instance_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return instance_dir


# ── Output rendering ───────────────────────────────────────────────────────

_CYAN = "\033[1;36m"
_GREEN = "\033[1;32m"
_YELLOW = "\033[33m"
_MAGENTA = "\033[35m"
_DIM = "\033[2m"
_RED = "\033[31m"
_RESET = "\033[0m"


def _render_event(event: dict) -> str | None:
    """Return a printable string for an outbox event, or None to skip."""
    etype = event.get("type")
    if etype == "agent":
        return f"{_CYAN}agent❯{_RESET} {event.get('content', '')}"
    if etype == "tool":
        name = event.get("name", "?")
        inp = event.get("input", {})
        return f"{_MAGENTA}  [tool] {name}({inp}){_RESET}"
    if etype == "heartbeat":
        return f"{_YELLOW}  [heartbeat] {event.get('content', '')}{_RESET}"
    if etype == "heartbeat_finished":
        return f"{_DIM}  [instance finished — all tasks done]{_RESET}"
    if etype == "status":
        return f"{_DIM}  [status: {event.get('value')}]{_RESET}"
    if etype == "error":
        return f"{_RED}  [error] {event.get('content')}{_RESET}"
    return None


# ── Chat session ───────────────────────────────────────────────────────────

async def _chat_session(instances_dir: Path, instance_id: str) -> None:
    from nutshell.core.ipc import FileIPC

    instance_dir = instances_dir / instance_id
    if not instance_dir.exists():
        print(f"Error: instance not found: {instance_id}", file=sys.stderr)
        sys.exit(1)

    ipc = FileIPC(instance_dir)

    print(f"{_DIM}instance : {instance_id}{_RESET}")
    print(f"{_DIM}dir      : {instance_dir}{_RESET}")
    print(f"{_DIM}commands : /kanban /instances /status /exit{_RESET}")
    print()

    # Replay full outbox history from the start
    outbox_offset = 0
    for event, offset in ipc.tail_outbox(0):
        outbox_offset = offset
        line = _render_event(event)
        if line:
            print(line)

    stop_event = asyncio.Event()
    session = PromptSession()

    with patch_stdout():
        await asyncio.gather(
            _input_loop(session, ipc, instance_dir, instances_dir, stop_event),
            _output_loop(ipc, outbox_offset, stop_event),
        )


async def _output_loop(
    ipc, offset: int, stop_event: asyncio.Event
) -> None:
    """Tail outbox and print new events until stop_event is set."""
    while not stop_event.is_set():
        for event, new_offset in ipc.tail_outbox(offset):
            offset = new_offset
            line = _render_event(event)
            if line:
                print(line)
        await asyncio.sleep(0.3)


async def _input_loop(
    session: "PromptSession",
    ipc,
    instance_dir: Path,
    instances_dir: Path,
    stop_event: asyncio.Event,
) -> None:
    """Read user input and dispatch to IPC or handle commands."""
    prompt_str = f"{_GREEN}you  ❯{_RESET} "

    while not stop_event.is_set():
        try:
            user_input = await session.prompt_async(ANSI(prompt_str))
        except (EOFError, KeyboardInterrupt):
            print(f"\n{_DIM}Bye! (server continues running){_RESET}")
            stop_event.set()
            return

        user_input = user_input.strip()
        if not user_input:
            continue

        # ── Built-in commands ─────────────────────────────────
        if user_input.lower() in ("/exit", "/quit", "/q"):
            print(f"{_DIM}Bye! (server continues running){_RESET}")
            stop_event.set()
            return

        if user_input.lower() == "/kanban":
            kanban_path = instance_dir / "kanban.md"
            content = kanban_path.read_text(encoding="utf-8").strip() if kanban_path.exists() else ""
            if content:
                print(f"{_YELLOW}  Kanban:\n{content}{_RESET}")
            else:
                print(f"{_YELLOW}  Kanban is empty.{_RESET}")
            continue

        if user_input.lower() == "/instances":
            _list_instances(instances_dir)
            continue

        if user_input.lower() == "/status":
            alive = ipc.is_daemon_alive()
            pid = ipc.read_pid()
            if alive:
                print(f"  server: running (pid {pid})")
            else:
                print(f"  server: not running — start with: nutshell-server")
            continue

        if user_input.startswith("/"):
            print(f"{_RED}  Unknown command: {user_input}{_RESET}")
            continue

        # ── Send to server ────────────────────────────────────
        ipc.send_message(user_input)


# ── Entry point ────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    instances_dir = Path(args.instances_dir)

    if args.list:
        _list_instances(instances_dir)
        return

    if args.create is not None:
        instance_id = args.create or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        _create_instance(instances_dir, instance_id, args.entity, args.heartbeat)
        print(f"Created instance: {instance_id}")
        asyncio.run(_chat_session(instances_dir, instance_id))
        return

    if args.attach:
        asyncio.run(_chat_session(instances_dir, args.attach))
        return

    # Default: create new instance with timestamp ID
    instance_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    _create_instance(instances_dir, instance_id, args.entity, args.heartbeat)
    print(f"Created instance: {instance_id}")
    asyncio.run(_chat_session(instances_dir, instance_id))


if __name__ == "__main__":
    main()
