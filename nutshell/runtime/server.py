"""Nutshell server — backend system.

Watches a _sessions/ directory and runs each discovered session as an
asyncio task. The server itself holds no hard-coded sessions; all sessions
are created by the chat UI writing a manifest.json.

Usage:
    nutshell-server                Start server (auto-daemonize)
    nutshell-server start          Same as above
    nutshell-server stop           Stop a running server
    nutshell-server status         Show server status
    nutshell-server update         Reinstall package and restart server
    nutshell-server --foreground   Run in foreground (no daemonize)

"""
import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SESSIONS_DIR = Path(__file__).parent.parent.parent / "sessions"
_SYSTEM_SESSIONS_DIR = Path(__file__).parent.parent.parent / "_sessions"
_REPO_ROOT = Path(__file__).parent.parent.parent


# ── PID file helpers ──────────────────────────────────────────────────────────

def _pid_file(system_dir: Path | None = None) -> Path:
    return (system_dir or _SYSTEM_SESSIONS_DIR) / "server.pid"


def _log_file(system_dir: Path | None = None) -> Path:
    return (system_dir or _SYSTEM_SESSIONS_DIR) / "server.log"


def _write_pid(system_dir: Path | None = None) -> None:
    pf = _pid_file(system_dir)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(str(os.getpid()))


def _read_pid(system_dir: Path | None = None) -> int | None:
    pf = _pid_file(system_dir)
    if not pf.exists():
        return None
    try:
        return int(pf.read_text().strip())
    except (ValueError, OSError):
        return None


def _clear_pid(system_dir: Path | None = None) -> None:
    try:
        _pid_file(system_dir).unlink(missing_ok=True)
    except OSError:
        pass


def _is_server_running(system_dir: Path | None = None) -> int | None:
    """Return PID if server is running, None otherwise."""
    pid = _read_pid(system_dir)
    if pid is None:
        return None
    try:
        os.kill(pid, 0)  # signal 0 = check existence
        return pid
    except (ProcessLookupError, PermissionError):
        _clear_pid(system_dir)
        return None


# ── Server core ───────────────────────────────────────────────────────────────

async def _run(sessions_dir: Path, system_sessions_dir: Path) -> None:
    from nutshell.runtime.watcher import SessionWatcher

    watcher = SessionWatcher(sessions_dir, system_sessions_dir)
    stop_event = asyncio.Event()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)

    _write_pid(system_sessions_dir)
    print(f"nutshell server started (pid={os.getpid()}). sessions dir: {sessions_dir.absolute()}")
    try:
        await watcher.run(stop_event)
    finally:
        _clear_pid(system_sessions_dir)
    print("nutshell server stopped.")


def _start_foreground(sessions_dir: Path, system_sessions_dir: Path) -> int:
    """Run server in the foreground."""
    sessions_dir.mkdir(parents=True, exist_ok=True)
    system_sessions_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(_run(sessions_dir, system_sessions_dir))
    return 0


def _start_daemon(sessions_dir: Path, system_sessions_dir: Path) -> int:
    """Launch server as a background daemon process."""
    existing = _is_server_running(system_sessions_dir)
    if existing:
        print(f"nutshell server already running (pid={existing}).")
        return 0

    system_sessions_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Launch a detached subprocess running this module in foreground mode
    cmd = [
        sys.executable, "-m", "nutshell.runtime.server",
        "--foreground",
        "--sessions-dir", str(sessions_dir),
        "--system-sessions-dir", str(system_sessions_dir),
    ]
    lf = _log_file(system_sessions_dir)
    log_fh = open(lf, "a")
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=log_fh,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # detach from parent
        cwd=str(_REPO_ROOT),
    )
    log_fh.close()  # parent no longer needs the fd

    # Wait briefly to confirm it started
    time.sleep(0.5)
    if proc.poll() is not None:
        print(f"Error: server exited immediately (code={proc.returncode}). Check {lf}")
        return 1

    print(f"nutshell server started in background (pid={proc.pid}). Log: {lf}")
    return 0


# ── Subcommands ───────────────────────────────────────────────────────────────

def _cmd_start(args) -> int:
    from nutshell.runtime.env import load_dotenv
    load_dotenv()

    sessions_dir = Path(args.sessions_dir)
    system_sessions_dir = Path(args.system_sessions_dir)

    if args.foreground:
        return _start_foreground(sessions_dir, system_sessions_dir)
    return _start_daemon(sessions_dir, system_sessions_dir)


def _system_dir_from_args(args) -> Path:
    return Path(getattr(args, "system_sessions_dir", str(_SYSTEM_SESSIONS_DIR)))


def _cmd_stop(args) -> int:
    sdir = _system_dir_from_args(args)
    pid = _is_server_running(sdir)
    if pid is None:
        print("nutshell server is not running.")
        return 0
    print(f"Stopping nutshell server (pid={pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_pid(sdir)
        print("Server already stopped.")
        return 0
    # Wait for graceful shutdown
    for _ in range(20):  # up to 10 seconds
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            _clear_pid(sdir)
            print("Server stopped.")
            return 0
    print(f"Warning: server (pid={pid}) did not stop within 10s. Sending SIGKILL...")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _clear_pid(sdir)
    print("Server killed.")
    return 0


def _cmd_status(args) -> int:
    sdir = _system_dir_from_args(args)
    pid = _is_server_running(sdir)
    if pid:
        print(f"nutshell server is running (pid={pid}).")
    else:
        print("nutshell server is not running.")
    return 0


def _cmd_update(args) -> int:
    """Reinstall the nutshell package and restart the server."""
    print("Stopping server...")
    _cmd_stop(args)

    print("Reinstalling nutshell...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", str(_REPO_ROOT)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"pip install failed:\n{result.stderr}")
        return 1
    print("Package reinstalled.")

    print("Restarting server...")
    return _cmd_start(args)


# ── CLI entry point ───────────────────────────────────────────────────────────

def _add_dir_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sessions-dir",
        default=str(SESSIONS_DIR),
        metavar="DIR",
        help=f"Session files directory (default: {SESSIONS_DIR})",
    )
    parser.add_argument(
        "--system-sessions-dir",
        default=str(_SYSTEM_SESSIONS_DIR),
        metavar="DIR",
        help=f"System session directory (default: {_SYSTEM_SESSIONS_DIR})",
    )


def main() -> None:
    # Shared flags — available on every subcommand and at top level.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--foreground", action="store_true",
                        help="Run in foreground (don't daemonize)")
    _add_dir_args(shared)

    parser = argparse.ArgumentParser(
        description="Nutshell server — backend system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
        parents=[shared],
    )
    parser.set_defaults(func=None)

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("start", allow_abbrev=False, parents=[shared],
                          help="Start the server (default)")
    subparsers.add_parser("stop", allow_abbrev=False, parents=[shared],
                          help="Stop the running server")
    subparsers.add_parser("status", allow_abbrev=False, parents=[shared],
                          help="Show server status")
    subparsers.add_parser("update", allow_abbrev=False, parents=[shared],
                          help="Reinstall package and restart server")

    args = parser.parse_args()

    # Map subcommand (or default) to handler
    _COMMANDS = {
        None: _cmd_start,
        "start": _cmd_start,
        "stop": _cmd_stop,
        "status": _cmd_status,
        "update": _cmd_update,
    }
    handler = _COMMANDS.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)
    sys.exit(handler(args))


if __name__ == "__main__":
    main()
