"""SessionWatcher: polls _sessions/ directory and manages server session tasks."""
from __future__ import annotations
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Callable

_AUTO_EXPIRE_HOURS = 5


class SessionWatcher:
    """Polling-based watcher for the sessions/ and _sessions/ directories.

    On each scan, discovers directories in _sessions/ with a manifest.json
    that have not yet been started (or have finished). Each discovered session
    is launched as an asyncio Task running Session.run_daemon_loop().
    """

    def __init__(
        self,
        sessions_dir: Path,
        system_sessions_dir: Path,
        agent_factory: Callable[[dict], object] | None = None,
    ) -> None:
        self.sessions_dir = sessions_dir
        self.system_sessions_dir = system_sessions_dir
        self._agent_factory = agent_factory
        self._active: dict[str, asyncio.Task] = {}  # session_id → task
        self._finished: set[str] = set()  # session_ids that have completed

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main watcher loop. Runs until stop_event is set."""
        print(f"[server] Watching: {self.system_sessions_dir.absolute()}")

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
        """Scan system_sessions_dir for new or recovered manifests. Returns newly started IDs."""
        if not self.system_sessions_dir.exists():
            return []
        discovered: list[str] = []

        for system_dir in sorted(self.system_sessions_dir.iterdir()):
            if not system_dir.is_dir():
                continue

            session_id = system_dir.name
            manifest_path = system_dir / "manifest.json"

            if not manifest_path.exists():
                continue

            # Skip already-finished sessions, unless the user explicitly
            # restarted them (status.json status set back to active).
            if session_id in self._finished:
                from nutshell.session_engine.session_status import read_session_status
                status_data = read_session_status(system_dir)
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

            from nutshell.session_engine.session_status import read_session_status, write_session_status, pid_alive
            status_data = read_session_status(system_dir)

            # Skip sessions whose daemon is already running (e.g. started by
            # `nutshell chat`). Without this check, the watcher would start a
            # competing daemon that races for the same events.jsonl queue.
            if pid_alive(status_data.get("pid")):
                continue

            if status_data.get("status") == "stopped":
                # Auto-expire sessions stopped for more than _AUTO_EXPIRE_HOURS
                stopped_at_str = status_data.get("stopped_at")
                auto_expired = False
                if stopped_at_str:
                    try:
                        stopped_at = datetime.fromisoformat(stopped_at_str)
                        now = datetime.now(stopped_at.tzinfo) if stopped_at.tzinfo is not None else datetime.now()
                        elapsed = (now - stopped_at).total_seconds()
                        if elapsed >= _AUTO_EXPIRE_HOURS * 3600:
                            write_session_status(system_dir, status="active", stopped_at=None)
                            from nutshell.session_engine.task_cards import clear_all_cards
                            tasks_dir = self.sessions_dir / session_id / "core" / "tasks"
                            clear_all_cards(tasks_dir)
                            print(f"[server] Auto-expired stopped session: {session_id}")
                            auto_expired = True
                    except Exception as e:
                        print(f"[watcher] Auto-expire error for {session_id}: {e}")
                if not auto_expired:
                    continue

            discovered.append(session_id)
            task = asyncio.create_task(
                self._start_session(session_id, system_dir, manifest),
                name=f"session-{session_id}",
            )
            self._active[session_id] = task

        return discovered

    async def _start_session(
        self, session_id: str, system_dir: Path, manifest: dict
    ) -> None:
        """Create a minimal agent from params and run server loop."""
        from nutshell.session_engine.session import Session
        from nutshell.runtime.ipc import FileIPC
        from nutshell.session_engine.session_status import read_session_status
        from nutshell.session_engine.session_config import read_config

        session_dir = self.sessions_dir / session_id

        # Read heartbeat_interval from core/config.yaml (source of truth).
        heartbeat = float(read_config(session_dir).get("heartbeat_interval") or 7200.0)

        try:
            if self._agent_factory is not None:
                agent = self._agent_factory(manifest)
            else:
                from nutshell.core.agent import Agent
                from nutshell.llm_engine.registry import resolve_provider

                params = read_config(session_dir)
                provider_str = (params.get("provider") or "anthropic").lower()
                provider = resolve_provider(provider_str)
                model = params.get("model") or None
                agent_kwargs: dict = {}
                if model:
                    agent_kwargs["model"] = model
                if params.get("fallback_model"):
                    agent_kwargs["fallback_model"] = params["fallback_model"]
                if params.get("fallback_provider"):
                    agent_kwargs["fallback_provider"] = params["fallback_provider"]
                agent = Agent(provider=provider, **agent_kwargs)
        except Exception as exc:
            print(f"[server] Failed to create agent for {session_id}: {exc}")
            # Mark as stopped so the watcher doesn't retry indefinitely
            from nutshell.session_engine.session_status import write_session_status
            write_session_status(system_dir, status="stopped")
            return

        ipc = FileIPC(system_dir)
        session = Session(
            agent,
            session_id=session_id,
            base_dir=self.sessions_dir,
            system_base=system_dir.parent,
            heartbeat=heartbeat,
        )

        # Always load history (needed for user messages even when tasks are empty)
        context_path = system_dir / "context.jsonl"
        if context_path.exists() and context_path.stat().st_size > 0:
            session.load_history()

        # Only announce if task cards have pending work — empty/idle sessions are silent
        from nutshell.session_engine.task_cards import has_pending_cards
        tasks_dir = session_dir / "core" / "tasks"
        if has_pending_cards(tasks_dir):
            print(f"[server] Resumed: {session_id} ({len(agent._history)} messages, tasks pending)")

        try:
            await session.run_daemon_loop(ipc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[server] Session {session_id} crashed: {exc}")
            ipc.append_event({"type": "error", "content": str(exc)})
