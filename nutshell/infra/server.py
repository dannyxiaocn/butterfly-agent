"""Nutshell server — backend system.

Watches an instances/ directory and runs each discovered instance as an
asyncio task. The server itself holds no hard-coded instances; all instances
are created by the chat UI writing a manifest.json.

Usage:
    python -m nutshell.infra.server
    python -m nutshell.infra.server --instances-dir ~/my-instances
    nutshell-server
"""
import argparse
import asyncio
import signal
from pathlib import Path

INSTANCES_DIR = Path("instances")


async def _run(instances_dir: Path) -> None:
    from nutshell.infra.watcher import InstanceWatcher

    watcher = InstanceWatcher(instances_dir)
    stop_event = asyncio.Event()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)

    print(f"nutshell server started. instances dir: {instances_dir.absolute()}")
    await watcher.run(stop_event)
    print("nutshell server stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Nutshell server — backend system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--instances-dir",
        default=str(INSTANCES_DIR),
        metavar="DIR",
        help=f"Directory to watch for instances (default: {INSTANCES_DIR})",
    )
    args = parser.parse_args()

    instances_dir = Path(args.instances_dir)
    instances_dir.mkdir(parents=True, exist_ok=True)

    asyncio.run(_run(instances_dir))


if __name__ == "__main__":
    main()
