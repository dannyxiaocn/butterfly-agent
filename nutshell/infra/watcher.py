"""InstanceWatcher: polls instances/ directory and manages server instance tasks."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
from typing import Callable


class InstanceWatcher:
    """Polling-based watcher for the instances/ directory.

    On each scan, discovers directories with a manifest.json that have not
    yet been started (or have finished). Each discovered instance is launched
    as an asyncio Task running Instance.run_daemon_loop().
    """

    def __init__(
        self,
        instances_dir: Path,
        agent_factory: Callable[[dict], object] | None = None,
    ) -> None:
        self.instances_dir = instances_dir
        self._agent_factory = agent_factory
        self._active: dict[str, asyncio.Task] = {}  # instance_id → task
        self._finished: set[str] = set()  # instance_ids that have completed

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main watcher loop. Runs until stop_event is set."""
        print(f"[server] Watching: {self.instances_dir.absolute()}")

        # Initial scan — recover existing instances
        discovered = await self._scan()
        if discovered:
            ids = ", ".join(discovered)
            print(f"[server] Discovered: {ids} [total {len(discovered)}]")

        while not stop_event.is_set():
            await asyncio.sleep(1.0)
            new = await self._scan()
            for iid in new:
                print(f"[server] Discovered: {iid}")

        # Cancel all active instance tasks on shutdown
        tasks = list(self._active.values())
        if tasks:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        print("[server] All instances stopped.")

    async def _scan(self) -> list[str]:
        """Scan instances_dir for new or recovered manifests. Returns newly started IDs."""
        if not self.instances_dir.exists():
            return []
        discovered: list[str] = []

        for instance_dir in sorted(self.instances_dir.iterdir()):
            if not instance_dir.is_dir():
                continue

            instance_id = instance_dir.name
            manifest_path = instance_dir / "manifest.json"

            if not manifest_path.exists():
                continue

            # Skip already-finished instances, unless the user explicitly
            # restarted them (manifest status set back to active).
            if instance_id in self._finished:
                try:
                    status = json.loads(manifest_path.read_text(encoding="utf-8")).get("status")
                except Exception:
                    continue
                if status == "stopped" or status is None:
                    continue
                # status is active — user wants to restart a crashed instance
                self._finished.discard(instance_id)
                print(f"[server] Restarting finished instance: {instance_id}")

            # Clean up finished tasks
            if instance_id in self._active:
                task = self._active[instance_id]
                if task.done():
                    exc = task.exception() if not task.cancelled() else None
                    if exc:
                        print(f"[server] Instance {instance_id} error: {exc}")
                    else:
                        print(f"[server] Instance finished: {instance_id}")
                    del self._active[instance_id]
                    self._finished.add(instance_id)
                continue  # already running

            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            if manifest.get("status") == "stopped":
                continue

            discovered.append(instance_id)
            task = asyncio.create_task(
                self._start_instance(instance_id, instance_dir, manifest),
                name=f"instance-{instance_id}",
            )
            self._active[instance_id] = task

        return discovered

    async def _start_instance(
        self, instance_id: str, instance_dir: Path, manifest: dict
    ) -> None:
        """Load agent from manifest and run server loop."""
        from nutshell.core.instance import Instance
        from nutshell.core.ipc import FileIPC

        heartbeat = float(manifest.get("heartbeat", 10.0))
        base_dir = instance_dir.parent

        try:
            if self._agent_factory is not None:
                agent = self._agent_factory(manifest)
            else:
                from nutshell import AgentLoader
                from nutshell.llm.anthropic import AnthropicProvider

                entity = manifest.get("entity", "entity/agent_core")
                entity_path = Path(entity)
                agent = AgentLoader().load(entity_path)
                agent._provider = AnthropicProvider()
        except Exception as exc:
            print(f"[server] Failed to load agent for {instance_id}: {exc}")
            return

        ipc = FileIPC(instance_dir)
        instance = Instance(agent, instance_id=instance_id, base_dir=base_dir, heartbeat=heartbeat)

        # Always load history (needed for user messages even when kanban is empty)
        context_path = instance_dir / "context.jsonl"
        if context_path.exists() and context_path.stat().st_size > 0:
            instance.load_history()

        # Only announce if kanban has pending work — empty/idle instances are silent
        kanban_path = instance_dir / "kanban.md"
        has_kanban = kanban_path.exists() and kanban_path.read_text(encoding="utf-8").strip()
        if has_kanban:
            print(f"[server] Resumed: {instance_id} ({len(agent._history)} messages, kanban pending)")

        try:
            await instance.run_daemon_loop(ipc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[server] Instance {instance_id} crashed: {exc}")
            ipc.append({"type": "error", "content": str(exc)})
