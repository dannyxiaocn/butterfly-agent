"""nutshell-chat — single-shot CLI for interacting with a Nutshell session.

Usage:
    nutshell-chat "message"                              # new session (default entity: agent)
    nutshell-chat --entity kimi_agent "message"          # new session, custom entity
    nutshell-chat --session <id> "message"               # continue existing session
    nutshell-chat --session <id> --no-wait "message"     # fire-and-forget
    nutshell-chat --session <id> --timeout 60 "message"  # custom timeout (seconds)

When creating a new session, the session ID is always printed on its own line:
    Session: <session_id>

Exit codes:
    0  — success
    1  — session not found, timeout, or startup failure
"""
from __future__ import annotations

import argparse
import json
import sys
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path


from nutshell.runtime.env import load_dotenv as _load_dotenv

_load_dotenv()

_DEFAULT_SYSTEM_BASE = Path(__file__).parent.parent.parent / "_sessions"
_DEFAULT_SESSIONS_BASE = Path(__file__).parent.parent.parent / "sessions"
_POLL_INTERVAL = 0.5


# ── Shared helpers ────────────────────────────────────────────────────────────

def _append_jsonl(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _send_message(ctx_path: Path, content: str) -> str:
    """Write user_input to context.jsonl, return msg_id."""
    msg_id = str(uuid.uuid4())
    _append_jsonl(ctx_path, {
        "type": "user_input",
        "content": content,
        "id": msg_id,
        "ts": datetime.now().isoformat(),
    })
    return msg_id


def _read_matching_turn(ctx_path: Path, msg_id: str) -> str | None:
    """Scan context.jsonl for a turn with user_input_id == msg_id.

    Returns the assistant text if found, None otherwise.
    Returns empty string if the turn exists but has no text.
    """
    if not ctx_path.exists():
        return None
    try:
        with ctx_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "turn":
                    continue
                if event.get("user_input_id") != msg_id:
                    continue
                for msg in reversed(event.get("messages", [])):
                    if msg.get("role") == "assistant":
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            return content
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    return block.get("text", "")
                return ""
    except Exception:
        return None
    return None


def _wait_for_reply(ctx_path: Path, msg_id: str, timeout: float) -> str | None:
    """Poll until a matching turn appears or timeout. Returns text or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = _read_matching_turn(ctx_path, msg_id)
        if reply is not None:
            return reply
        time.sleep(_POLL_INTERVAL)
    return None


# ── Continue existing session ─────────────────────────────────────────────────

def _continue_session(
    session_id: str,
    message: str,
    *,
    no_wait: bool,
    timeout: float,
    system_base: Path,
) -> int:
    """Handle --session <id> mode. Returns exit code."""
    system_dir = system_base / session_id
    if not (system_dir / "manifest.json").exists():
        print(
            f"Error: session '{session_id}' not found in {system_base}",
            file=sys.stderr,
        )
        return 1

    ctx_path = system_dir / "context.jsonl"
    msg_id = _send_message(ctx_path, message)

    if no_wait:
        return 0

    reply = _wait_for_reply(ctx_path, msg_id, timeout)
    if reply is None:
        print(f"Error: no response within {timeout:.0f}s", file=sys.stderr)
        return 1

    print(reply)
    return 0


# ── New session ───────────────────────────────────────────────────────────────

def _new_session(
    entity_name: str,
    message: str,
    *,
    no_wait: bool,
    timeout: float,
    system_base: Path,
    sessions_base: Path,
    inject_memory: dict[str, str] | None = None,
    keep_alive: bool = False,
) -> int:
    """Handle new-session mode. Spawns daemon thread, returns exit code."""
    import asyncio
    from nutshell.llm_engine.loader import AgentLoader
    from nutshell.runtime.session import Session
    from nutshell.runtime.ipc import FileIPC
    from nutshell.runtime.session_factory import init_session

    entity_base = Path(__file__).parent.parent.parent / "entity"
    try:
        agent = AgentLoader().load(entity_base / entity_name)
    except Exception as exc:
        print(f"Error: failed to load entity '{entity_name}': {exc}", file=sys.stderr)
        return 1

    session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    # Populate core/ with entity files so _load_session_capabilities() finds them
    try:
        init_session(
            session_id=session_id,
            entity_name=entity_name,
            sessions_base=sessions_base,
            system_sessions_base=system_base,
            entity_base=entity_base,
        )
    except Exception as exc:
        print(f"Error: failed to initialise session: {exc}", file=sys.stderr)
        return 1

    # Write injected memory layers (before daemon starts reading memory)
    if inject_memory:
        mem_dir = sessions_base / session_id / "core" / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        for key, value in inject_memory.items():
            (mem_dir / f"{key}.md").write_text(value, encoding="utf-8")

    session = Session(agent, session_id=session_id, base_dir=sessions_base, system_base=system_base)
    ipc = FileIPC(session.system_dir)

    # ready_event: set by the daemon once it has recorded input_offset.
    # We MUST NOT write user_input before this point, or the daemon will
    # skip it (input_offset is captured at daemon startup via context_size()).
    ready_event = threading.Event()
    stop_event_holder: list = []  # filled once asyncio loop is running

    def _run_daemon() -> None:
        import asyncio as _asyncio

        async def _async() -> None:
            stop_ev = _asyncio.Event()
            stop_event_holder.append(stop_ev)

            # Patch context_size() to signal readiness on first call
            original_ctx_size = ipc.context_size
            patched = False

            def _patched_ctx_size() -> int:
                nonlocal patched
                result = original_ctx_size()
                if not patched:
                    patched = True
                    ready_event.set()
                return result

            ipc.context_size = _patched_ctx_size  # type: ignore[method-assign]
            await session.run_daemon_loop(ipc, stop_event=stop_ev)

        _asyncio.run(_async())

    daemon_thread = threading.Thread(target=_run_daemon, daemon=True)
    daemon_thread.start()

    # Wait for daemon to record input_offset
    if not ready_event.wait(timeout=10.0):
        print("Error: daemon failed to start within 10s", file=sys.stderr)
        return 1

    ctx_path = session.system_dir / "context.jsonl"
    msg_id = _send_message(ctx_path, message)

    if no_wait:
        print(f"Session: {session_id}")
        _stop_daemon(stop_event_holder, daemon_thread)
        return 0

    reply = _wait_for_reply(ctx_path, msg_id, timeout)

    if keep_alive:
        # Stop the in-process daemon but launch a background server
        _stop_daemon(stop_event_holder, daemon_thread)
        subprocess.Popen(
            ["nutshell-server"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if reply is None:
            print(f"Error: no response within {timeout:.0f}s", file=sys.stderr)
            print(f"\nSession: {session_id}")
            print("[heartbeat active — server running in background]")
            return 1
        print(reply)
        print(f"\nSession: {session_id}")
        print("[heartbeat active — server running in background]")
        return 0
    else:
        _stop_daemon(stop_event_holder, daemon_thread)
        if reply is None:
            print(f"Error: no response within {timeout:.0f}s", file=sys.stderr)
            print(f"\nSession: {session_id}")
            return 1
        print(reply)
        print(f"\nSession: {session_id}")
        return 0


def _stop_daemon(
    stop_event_holder: list,
    daemon_thread: threading.Thread,
    join_timeout: float = 5.0,
) -> None:
    """Signal daemon to stop and wait for thread to finish."""
    if stop_event_holder:
        stop_ev = stop_event_holder[0]
        # stop_ev lives in an asyncio loop on the daemon thread;
        # call_soon_threadsafe would be ideal, but we can't easily get the loop.
        # Setting a threading.Event equivalent works since run_daemon_loop
        # checks asyncio.Event.is_set() which is thread-safe for reads.
        try:
            stop_ev.set()
        except RuntimeError:
            pass  # event loop may already be closed
    daemon_thread.join(timeout=join_timeout)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nutshell-chat",
        description="Send a message to a Nutshell session and print the response.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  nutshell-chat 'Plan a data pipeline'\n"
            "  nutshell-chat --entity kimi_agent 'Review this code'\n"
            "  nutshell-chat --session 2026-03-24_10-00-00 'Status update?'\n"
            "  nutshell-chat --session <id> --no-wait 'Run overnight report'\n"
        ),
    )
    parser.add_argument("message", help="Message to send to the agent")
    parser.add_argument(
        "--session", metavar="ID",
        help="Continue an existing session by ID",
    )
    parser.add_argument(
        "--entity", default="agent", metavar="NAME",
        help="Entity to use for a new session (default: agent)",
    )
    parser.add_argument(
        "--no-wait", action="store_true",
        help="Send without waiting for a response",
    )
    parser.add_argument(
        "--timeout", type=float, default=300.0,
        help="Seconds to wait for response (default: 300)",
    )
    # Hidden args for testing path overrides
    parser.add_argument("--system-base", type=Path, default=_DEFAULT_SYSTEM_BASE,
                        help=argparse.SUPPRESS)
    parser.add_argument("--sessions-base", type=Path, default=_DEFAULT_SESSIONS_BASE,
                        help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.session:
        code = _continue_session(
            args.session,
            args.message,
            no_wait=args.no_wait,
            timeout=args.timeout,
            system_base=args.system_base,
        )
    else:
        code = _new_session(
            args.entity,
            args.message,
            no_wait=args.no_wait,
            timeout=args.timeout,
            system_base=args.system_base,
            sessions_base=args.sessions_base,
            keep_alive=getattr(args, 'keep_alive', False),
        )

    sys.exit(code)


if __name__ == "__main__":
    main()
