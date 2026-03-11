"""Nutshell server — backend system.

Watches a sessions/ directory and runs each discovered session as an
asyncio task. The server itself holds no hard-coded sessions; all sessions
are created by the chat UI writing a manifest.json.

Usage:
    python -m nutshell.runtime.server
    python -m nutshell.runtime.server --sessions-dir ~/my-sessions
    nutshell-server
"""
import argparse
import asyncio
import signal
from pathlib import Path

SESSIONS_DIR = Path("sessions")


async def _run(sessions_dir: Path) -> None:
    from nutshell.runtime.watcher import SessionWatcher

    watcher = SessionWatcher(sessions_dir)
    stop_event = asyncio.Event()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)

    print(f"nutshell server started. sessions dir: {sessions_dir.absolute()}")
    await watcher.run(stop_event)
    print("nutshell server stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Nutshell server — backend system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--sessions-dir",
        default=str(SESSIONS_DIR),
        metavar="DIR",
        help=f"Directory to watch for sessions (default: {SESSIONS_DIR})",
    )
    args = parser.parse_args()

    sessions_dir = Path(args.sessions_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)

    asyncio.run(_run(sessions_dir))


if __name__ == "__main__":
    main()
