"""Example 06: Instance with heartbeat via server mode.

Shows how to create an instance manifest, let the nutshell server manage
the agent loop, and observe output through FileIPC.

Prerequisites:
    nutshell-server &       # or: python -m nutshell.infra.server &

Then run this script:
    python examples/06_heartbeat_agent.py
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path

INSTANCES_DIR = Path("instances")


async def main():
    # Create instance manifest — server picks it up automatically
    instance_id = f"heartbeat-demo-{datetime.now().strftime('%H-%M-%S')}"
    instance_dir = INSTANCES_DIR / instance_id
    instance_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "instance_id": instance_id,
        "entity": "entity/agent_core",
        "heartbeat": 8.0,
        "created_at": datetime.now().isoformat(),
    }
    (instance_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    # Seed kanban directly — server will process it on next heartbeat
    (instance_dir / "kanban.md").write_text(
        "- Summarize recursion in one sentence\n"
        "- List 3 benefits of async programming\n"
        "- Time complexity of binary search?\n",
        encoding="utf-8",
    )

    print(f"Instance created: {instance_id}")
    print(f"Directory:        {instance_dir.absolute()}")
    print("Watching context for server output (Ctrl+C to stop)...\n")

    # Tail context.jsonl — observe server output as it arrives
    from nutshell.core.ipc import FileIPC

    ipc = FileIPC(instance_dir)
    offset = 0

    try:
        while True:
            for event, new_offset in ipc.tail_display(offset):
                offset = new_offset
                etype = event.get("type")
                if etype == "agent":
                    print(f"[agent]     {event.get('content', '')}")
                elif etype == "tool":
                    print(f"[tool]      {event.get('name')}({event.get('input')})")
                elif etype == "heartbeat":
                    print(f"[heartbeat] {event.get('content', '')}")
                elif etype == "heartbeat_finished":
                    print("[done]      All tasks complete.")
                    return
                elif etype == "status":
                    print(f"[status]    {event.get('value')}")
                elif etype == "error":
                    print(f"[error]     {event.get('content')}")

            await asyncio.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopped watching. Instance continues running on the server.")


if __name__ == "__main__":
    asyncio.run(main())
