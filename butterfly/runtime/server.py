"""Butterfly server — backend system.

Watches a _sessions/ directory and runs each discovered session as an
asyncio task. The server itself holds no hard-coded sessions; all sessions
are created by the chat UI writing a manifest.json.

Not invoked directly as a CLI entrypoint. The unified `butterfly` command
boots the server+web pair (see `ui/cli/main.py::cmd_default`). The daemon
helpers exposed here (`_start_daemon`, `_is_server_running`, `_cmd_stop`)
are reused by `butterfly update` and by `_ensure_server_running` in the
session subcommands. `python -m butterfly.runtime.server --foreground` is
the process image spawned by `_start_daemon` and by `os.execvp` during
auto-update respawn.

"""
import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
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

def _update_status_path(system_sessions_dir: Path) -> Path:
    return system_sessions_dir / "update_status.json"


def _consume_stale_reload_flag(system_sessions_dir: Path) -> None:
    """Drop `reload: true` from the status file (or delete it outright).

    Called once at server startup. If the previous process image wrote the
    flag before `os.execvp`, the respawn itself has already honoured the
    reload request; leaving the flag in place would make every fresh page
    poll re-trigger `window.location.reload()` on the frontend.
    """
    path = _update_status_path(system_sessions_dir)
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Malformed file — safest is to drop it; worker will re-emit if needed.
        try:
            path.unlink()
        except OSError:
            pass
        return
    if not isinstance(payload, dict) or not payload.get("reload"):
        return
    # Preserve the audit trail (new_head / applied_at) but drop the reload flag
    # so subsequent polls don't refresh the page.
    payload.pop("reload", None)
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def _git(*args: str, check: bool = False, capture: bool = False,
         timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(_REPO_ROOT), *args],
        check=check,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


async def _auto_update_worker(
    interval_sec: int,
    system_sessions_dir: Path,
    stop_event: asyncio.Event,
) -> None:
    """Background worker: hourly check for upstream updates.

    Behavior:
      - Clean tree + new commits: runs `git pull --ff-only` + `pip install -e .`
        + frontend rebuild, writes `update_status.json` with `applied=true`,
        then `os.execvp`s self with the updated code. Web UI polls the status
        file and force-reloads on seeing the new `applied_at`.
      - Dirty tree + new commits: writes `update_status.json` with
        `dirty=true` + `available=true`. Web UI shows a top-right
        notification; no auto-apply (user runs `butterfly update` manually
        after committing).
      - No new commits: clears any stale `update_status.json`.

    Set ``BUTTERFLY_AUTOUPDATE_INTERVAL_SEC=0`` to disable the worker.

    All blocking subprocess calls run on the default executor so the
    SessionWatcher's polling loop is never starved (git fetch can take tens
    of seconds over a slow link).
    """
    status_path = _update_status_path(system_sessions_dir)

    def _sync_check_and_apply() -> str | None:
        """Runs the whole check/apply pipeline in a worker thread.

        Returns None on happy path (updates applied up to execvp or no-op);
        returns a non-empty string to surface an error message to the async
        caller without raising.
        """
        _git("fetch", "--quiet", "origin", timeout=60)
        head = _git("rev-parse", "HEAD", capture=True).stdout.strip()
        remote = _git("rev-parse", "origin/main", capture=True).stdout.strip()
        if not head or not remote or head == remote:
            if status_path.exists():
                try:
                    status_path.unlink()
                except OSError:
                    pass
            return None

        # `git status --porcelain` covers modified + staged + untracked in one
        # probe. Must match `cmd_update`'s detector exactly — otherwise the
        # worker thinks the tree is clean, tries `git pull --ff-only`, Git
        # refuses because an untracked file collides with an incoming one,
        # and the failure is logged silently instead of becoming a UI banner.
        porcelain = _git("status", "--porcelain", capture=True).stdout
        dirty = bool(porcelain.strip())
        commits_behind = int(
            _git("rev-list", "--count", f"{head}..{remote}",
                 capture=True).stdout.strip() or "0"
        )

        if dirty:
            status_path.write_text(json.dumps({
                "available": True,
                "dirty": True,
                "commits_behind": commits_behind,
                "local_head": head,
                "remote_head": remote,
                "checked_at": _now_iso(),
            }))
            return None

        print(f"[auto-update] Applying {commits_behind} upstream commits...", flush=True)
        pull = _git("pull", "--ff-only", timeout=120)
        if pull.returncode != 0:
            return "git pull failed"

        pip = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", str(_REPO_ROOT)],
            capture_output=True, text=True, timeout=300,
        )
        if pip.returncode != 0:
            return f"pip install failed:\n{pip.stderr}"

        frontend_dir = _REPO_ROOT / "ui" / "web" / "frontend"
        if (frontend_dir / "package.json").exists():
            fb = subprocess.run(
                ["npm", "run", "build"],
                cwd=str(frontend_dir), capture_output=True, text=True,
                timeout=300,
            )
            if fb.returncode != 0:
                print(f"[auto-update] frontend rebuild failed: {fb.stderr[:200]}", flush=True)

        status_path.write_text(json.dumps({
            "applied": True,
            "new_head": remote,
            "applied_at": _now_iso(),
            "reload": True,
        }))

        print("[auto-update] Respawning server with new code...", flush=True)
        _clear_pid(system_sessions_dir)
        cmd = [
            sys.executable, "-m", "butterfly.runtime.server",
            "--foreground",
            "--sessions-dir", str(SESSIONS_DIR),
            "--system-sessions-dir", str(system_sessions_dir),
        ]
        os.execvp(sys.executable, cmd)
        return None  # unreachable — execvp replaces process image

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
            return
        except asyncio.TimeoutError:
            pass

        try:
            err = await asyncio.to_thread(_sync_check_and_apply)
            if err:
                print(f"[auto-update] {err}", flush=True)
        except Exception as e:  # noqa: BLE001 — keep worker alive on any error
            print(f"[auto-update] error: {e}", flush=True)


async def _run(sessions_dir: Path, system_sessions_dir: Path) -> None:
    from butterfly.runtime.watcher import SessionWatcher

    watcher = SessionWatcher(sessions_dir, system_sessions_dir)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)

    _write_pid(system_sessions_dir)
    print(f"butterfly server started (pid={os.getpid()}). sessions dir: {sessions_dir.absolute()}")

    # Post-execvp respawn arrives here with `update_status.json` still carrying
    # `reload: true`. The respawn itself *is* the reload having been honoured,
    # so we must drop the flag before the frontend polls `/api/update_status`
    # — otherwise it observes `reload: true` on every page load until the
    # worker's next iteration (default 3600 s) and loops the browser tab.
    # Fresh-start (no upstream update yet) simply has no file to touch.
    _consume_stale_reload_flag(system_sessions_dir)

    interval = int(os.environ.get("BUTTERFLY_AUTOUPDATE_INTERVAL_SEC", "3600"))
    watcher_task = asyncio.create_task(watcher.run(stop_event))
    tasks: list[asyncio.Task] = [watcher_task]
    if interval > 0 and (_REPO_ROOT / ".git").exists():
        tasks.append(asyncio.create_task(
            _auto_update_worker(interval, system_sessions_dir, stop_event)
        ))

    try:
        # Surface crashes — if the watcher task raises, don't let the server
        # keep running as a zombie with the PID file held; propagate so the
        # `finally` block clears the PID and the process exits non-zero.
        # `wait(FIRST_EXCEPTION)` catches any task failure (watcher or
        # auto-update) and cancels the rest so exit is prompt.
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_EXCEPTION,
        )
        for p in pending:
            p.cancel()
        # Await cancellations so they clean up before we drop the PID.
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Re-raise the first task exception, if any, to exit non-zero.
        for d in done:
            exc = d.exception()
            if exc is not None:
                raise exc
    finally:
        _clear_pid(system_sessions_dir)
    print("butterfly server stopped.")


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
        print(f"butterfly server already running (pid={existing}).")
        return 0

    system_sessions_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Launch a detached subprocess running this module in foreground mode
    cmd = [
        sys.executable, "-m", "butterfly.runtime.server",
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

    print(f"butterfly server started in background (pid={proc.pid}). Log: {lf}")
    return 0


# ── Subcommands ───────────────────────────────────────────────────────────────

def _cmd_start(args) -> int:
    from butterfly.runtime.env import load_dotenv
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
        print("butterfly server is not running.")
        return 0
    print(f"Stopping butterfly server (pid={pid})...")
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
        print(f"butterfly server is running (pid={pid}).")
    else:
        print("butterfly server is not running.")
    return 0


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
        description="Butterfly server — backend system",
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
    args = parser.parse_args()

    # Map subcommand (or default) to handler
    _COMMANDS = {
        None: _cmd_start,
        "start": _cmd_start,
        "stop": _cmd_stop,
        "status": _cmd_status,
    }
    handler = _COMMANDS.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)
    sys.exit(handler(args))


if __name__ == "__main__":
    main()
