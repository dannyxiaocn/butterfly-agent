"""SessionWatcher: polls sessions/ directory and manages server session tasks."""
from __future__ import annotations
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Callable

_AUTO_EXPIRE_HOURS = 5


class SessionWatcher:
    """Polling-based watcher for the sessions/ directory.

    On each scan, discovers directories with a manifest.json that have not
    yet been started (or have finished). Each discovered session is launched
    as an asyncio Task running Session.run_daemon_loop().
    """

    def __init__(
        self,
        sessions_dir: Path,
        agent_factory: Callable[[dict], object] | None = None,
    ) -> None:
        self.sessions_dir = sessions_dir
        self._agent_factory = agent_factory
        self._active: dict[str, asyncio.Task] = {}  # session_id → task
        self._finished: set[str] = set()  # session_ids that have completed

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main watcher loop. Runs until stop_event is set."""
        print(f"[server] Watching: {self.sessions_dir.absolute()}")

        # Initial scan — recover existing sessions
        discovered = await self._scan()
        if discovered:
            ids = ", ".join(discovered)
            print(f"[server] Discovered: {ids} [total {len(discovered)}]")

        while not stop_event.is_set():
            await asyncio.sleep(1.0)
            new = await self._scan()
            for sid in new:
                print(f"[server] Discovered: {sid}")

        # Cancel all active session tasks on shutdown
        tasks = list(self._active.values())
        if tasks:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        print("[server] All sessions stopped.")

    async def _scan(self) -> list[str]:
        """Scan sessions_dir for new or recovered manifests. Returns newly started IDs."""
        if not self.sessions_dir.exists():
            return []
        discovered: list[str] = []

        for session_dir in sorted(self.sessions_dir.iterdir()):
            if not session_dir.is_dir():
                continue

            session_id = session_dir.name
            manifest_path = session_dir / "manifest.json"

            if not manifest_path.exists():
                continue

            # Skip already-finished sessions, unless the user explicitly
            # restarted them (status.json status set back to active).
            if session_id in self._finished:
                from nutshell.runtime.status import read_session_status
                status_data = read_session_status(session_dir)
                status = status_data.get("status", "active")
                if status == "stopped":
                    continue
                # status is active — user wants to restart a crashed session
                self._finished.discard(session_id)
                print(f"[server] Restarting finished session: {session_id}")

            # Clean up finished tasks
            if session_id in self._active:
                task = self._active[session_id]
                if task.done():
                    exc = task.exception() if not task.cancelled() else None
                    if exc:
                        print(f"[server] Session {session_id} error: {exc}")
                    else:
                        print(f"[server] Session finished: {session_id}")
                    del self._active[session_id]
                    self._finished.add(session_id)
                continue  # already running

            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            from nutshell.runtime.status import read_session_status, write_session_status
            status_data = read_session_status(session_dir)
            if status_data.get("status") == "stopped":
                # Auto-expire sessions stopped for more than _AUTO_EXPIRE_HOURS
                stopped_at_str = status_data.get("stopped_at")
                auto_expired = False
                if stopped_at_str:
                    try:
                        elapsed = (datetime.now() - datetime.fromisoformat(stopped_at_str)).total_seconds()
                        if elapsed >= _AUTO_EXPIRE_HOURS * 3600:
                            write_session_status(session_dir, status="active", stopped_at=None)
                            tasks_path = session_dir / "tasks.md"
                            if tasks_path.exists():
                                tasks_path.write_text("", encoding="utf-8")
                            print(f"[server] Auto-expired stopped session: {session_id}")
                            auto_expired = True
                    except Exception:
                        pass
                if not auto_expired:
                    continue

            discovered.append(session_id)
            task = asyncio.create_task(
                self._start_session(session_id, session_dir, manifest),
                name=f"session-{session_id}",
            )
            self._active[session_id] = task

        return discovered

    async def _start_session(
        self, session_id: str, session_dir: Path, manifest: dict
    ) -> None:
        """Load agent from manifest and run server loop."""
        from nutshell.runtime.session import Session
        from nutshell.runtime.ipc import FileIPC

        heartbeat = float(manifest.get("heartbeat", 10.0))
        base_dir = session_dir.parent

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
            print(f"[server] Failed to load agent for {session_id}: {exc}")
            return

        ipc = FileIPC(session_dir)
        session = Session(agent, session_id=session_id, base_dir=base_dir, heartbeat=heartbeat)

        # Always load history (needed for user messages even when tasks are empty)
        context_path = session_dir / "context.jsonl"
        if context_path.exists() and context_path.stat().st_size > 0:
            session.load_history()

        # Only announce if tasks have pending work — empty/idle sessions are silent
        tasks_path = session_dir / "tasks.md"
        has_tasks = tasks_path.exists() and tasks_path.read_text(encoding="utf-8").strip()
        if has_tasks:
            print(f"[server] Resumed: {session_id} ({len(agent._history)} messages, tasks pending)")

        try:
            await session.run_daemon_loop(ipc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[server] Session {session_id} crashed: {exc}")
            ipc.append({"type": "error", "content": str(exc)})
