"""Example 07: Daemon + multi-instance workflow.

This example shows how the nutshell daemon and chat UI work together:

    Terminal 1 — start the daemon:
        python -m nutshell.daemon

    Terminal 2 — create an instance and chat:
        python chat.py --create demo --entity entity/agent_core

    Terminal 2 — re-attach (full history replayed from context.jsonl):
        python chat.py --attach demo

    Terminal 2 — list all instances:
        python chat.py --list

You can also drive the daemon programmatically:
"""
import asyncio
import json
from pathlib import Path
from datetime import datetime


def create_manifest(instances_dir: Path, instance_id: str, entity: str = "entity/agent_core") -> None:
    """Write a manifest.json that the daemon will discover and start."""
    instance_dir = instances_dir / instance_id
    instance_dir.mkdir(parents=True, exist_ok=True)
    (instance_dir / "files").mkdir(exist_ok=True)

    manifest = {
        "instance_id": instance_id,
        "entity": entity,
        "created_at": datetime.now().isoformat(),
        "heartbeat": 10.0,
    }
    (instance_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(f"Created manifest: {instance_dir / 'manifest.json'}")


async def send_and_watch(instances_dir: Path, instance_id: str, message: str) -> None:
    """Send a message to a running instance and watch context for the reply."""
    from nutshell.core.ipc import FileIPC

    instance_dir = instances_dir / instance_id
    ipc = FileIPC(instance_dir)

    offset = ipc.size()
    msg_id = ipc.send_message(message)
    print(f"Sent message [{msg_id[:8]}]: {message!r}")
    print("Waiting for reply...")

    # Poll context for the reply
    for _ in range(30):  # up to 15 seconds
        await asyncio.sleep(0.5)
        for event, new_offset in ipc.tail_display(offset):
            offset = new_offset
            if event.get("type") == "agent":
                print(f"Reply: {event['content']}")
                return
    print("No reply received within timeout.")


if __name__ == "__main__":
    instances_dir = Path("instances")

    # Create a demo instance manifest (daemon will pick it up automatically)
    instance_id = "demo-" + datetime.now().strftime("%H-%M-%S")
    create_manifest(instances_dir, instance_id)

    print()
    print("Now start the daemon in another terminal:")
    print("    python -m nutshell.daemon")
    print()
    print("Then attach to this instance:")
    print(f"    python chat.py --attach {instance_id}")
    print()
    print("Or send a message programmatically:")
    print(f"    asyncio.run(send_and_watch(Path('instances'), '{instance_id}', 'hello'))")
