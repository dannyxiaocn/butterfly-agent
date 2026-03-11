from __future__ import annotations
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from nutshell.core.agent import Agent
from nutshell.core.tool import tool
from nutshell.core.types import AgentResult

if TYPE_CHECKING:
    from nutshell.core.ipc import FileIPC

INSTANCES_DIR = Path("instances")
DEFAULT_HEARTBEAT_INTERVAL = 10.0  # seconds
INSTANCE_FINISHED = "INSTANCE_FINISHED"


class Instance:
    """Agent persistent run context (server mode only).

    Disk layout: instances/<id>/
        manifest.json    — config + runtime state (entity, heartbeat, status, pid)
        kanban.md        — free-form task notes (plain file read/write)
        context.jsonl    — append-only log: user_input, turn, status, error, heartbeat_finished
        files/           — associated files directory

    Usage:
        inst = Instance(agent, instance_id="my-project")
        ipc  = FileIPC(inst.instance_dir)
        await inst.run_daemon_loop(ipc)

    Resuming an existing instance uses the same constructor — directory
    creation is idempotent (existing files are never overwritten).
    """

    def __init__(
        self,
        agent: Agent,
        instance_id: str | None = None,
        base_dir: Path = INSTANCES_DIR,
        heartbeat: float = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        self._agent = agent
        self._instance_id = instance_id or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._base_dir = base_dir
        self._heartbeat_interval = heartbeat
        self._agent_lock: asyncio.Lock = asyncio.Lock()
        self._ipc: FileIPC | None = None

        # Idempotent directory creation — safe for both new and resumed instances
        self.instance_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(exist_ok=True)
        if not self.kanban_path.exists():
            self.kanban_path.write_text("", encoding="utf-8")
        if not self._context_path.exists():
            self._context_path.touch()

        self._inject_kanban_tools(agent)

    def _inject_kanban_tools(self, agent: Agent) -> None:
        kanban_path = self.kanban_path

        @tool(description="Read the current kanban board")
        def read_kanban() -> str:
            content = kanban_path.read_text(encoding="utf-8").strip()
            return content or "(empty)"

        @tool(description="Overwrite the kanban board. Pass empty string to clear all tasks.")
        def write_kanban(content: str) -> str:
            kanban_path.write_text(content, encoding="utf-8")
            return "Kanban updated."

        agent.tools.extend([read_kanban, write_kanban])

    # ── History persistence ────────────────────────────────────────

    def load_history(self) -> None:
        """Restore agent._history from context.jsonl on resume.

        Reads "turn" events in order, flattening their messages into
        agent._history. Preserves full Anthropic-format content including
        tool_use IDs and tool_result blocks.
        """
        if not self._context_path.exists():
            return
        from nutshell.core.types import Message
        history: list[Message] = []
        try:
            with self._context_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        if event.get("type") == "turn":
                            for m in event.get("messages", []):
                                history.append(Message(role=m["role"], content=m["content"]))
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass
        self._agent._history = history

    # ── Activation ────────────────────────────────────────────────

    async def chat(self, message: str) -> AgentResult:
        """Run agent with user message. Holds agent lock — blocks heartbeat tick."""
        old_len = len(self._agent._history)
        async with self._agent_lock:
            result = await self._agent.run(message)

        # Append full turn (the user_input event was already written by the UI
        # via send_message before the server picked it up).
        self._append_context({
            "type": "turn",
            "triggered_by": "user",
            "messages": [{"role": m.role, "content": m.content} for m in result.messages[old_len:]],
        })
        return result

    async def tick(self) -> AgentResult | None:
        """Single heartbeat: run agent if kanban is non-empty.

        Returns None if kanban is empty.
        Clears kanban and prunes history if agent responds INSTANCE_FINISHED.
        """
        kanban_content = self.kanban_path.read_text(encoding="utf-8").strip()
        if not kanban_content:
            return None

        # Snapshot history so we can roll back if INSTANCE_FINISHED
        history_snapshot = list(self._agent._history)
        old_len = len(self._agent._history)

        heartbeat_instructions = self._agent.heartbeat_prompt or "Continue working on your tasks."
        prompt = f"Heartbeat activation.\n\nCurrent kanban:\n{kanban_content}\n\n{heartbeat_instructions}"

        async with self._agent_lock:
            result = await self._agent.run(prompt)

        if INSTANCE_FINISHED in result.content:
            # Clear kanban, prune heartbeat history so it doesn't pollute context
            self.kanban_path.write_text("", encoding="utf-8")
            self._agent._history = history_snapshot
            self._append_context({"type": "heartbeat_finished"})
        else:
            # Only log to context if instance is still active — skip if user stopped
            # the instance while this heartbeat was in-flight (avoids ghost output in UI)
            if not self.is_stopped():
                self._append_context({
                    "type": "turn",
                    "triggered_by": "heartbeat",
                    "messages": [{"role": m.role, "content": m.content} for m in result.messages[old_len:]],
                })

        return result

    # ── Stop / Start ───────────────────────────────────────────────

    @property
    def manifest_path(self) -> Path:
        return self.instance_dir / "manifest.json"

    def is_stopped(self) -> bool:
        """True if manifest has status=stopped."""
        if not self.manifest_path.exists():
            return False
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            return manifest.get("status") == "stopped"
        except Exception:
            return False

    def set_status(self, status: str) -> None:
        """Write status field to manifest.json."""
        if not self.manifest_path.exists():
            return
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = status
            self.manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    def _write_pid(self) -> None:
        """Write current process PID into manifest.json."""
        if not self.manifest_path.exists():
            return
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            manifest["pid"] = os.getpid()
            self.manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    def _clear_pid(self) -> None:
        """Clear PID from manifest.json when daemon stops."""
        if not self.manifest_path.exists():
            return
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            manifest["pid"] = None
            self.manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    # ── Server loop ────────────────────────────────────────────────

    async def run_daemon_loop(self, ipc: "FileIPC") -> None:
        """Run as a server-managed instance.

        Polls context.jsonl for user_input events every 0.5s.
        Fires heartbeat ticks every heartbeat_interval seconds.

        Heartbeat is skipped when:
          - instance status == "stopped" (user issued /stop)
          - agent_lock is held (agent already running)

        A user message always wakes a stopped instance (clears stopped status).
        last_tick_time is updated AFTER the tick completes, so tick duration
        never eats into the next interval.
        """
        self._ipc = ipc
        self._write_pid()

        # Skip existing context events — only process new user_input events.
        # Starting at current file size prevents replay of prior session messages.
        input_offset = ipc.size()
        last_tick_time = asyncio.get_event_loop().time()

        try:
            while True:
                # Poll for new user_input events
                inputs, input_offset = ipc.poll_inputs(input_offset)
                for msg in inputs:
                    content = msg.get("content", "")
                    # User message wakes a stopped instance
                    if self.is_stopped():
                        self.set_status("active")
                        self._append_context({"type": "status", "value": "resumed"})
                    try:
                        await self.chat(content)
                    except Exception as exc:
                        self._append_context({"type": "error", "content": str(exc)})

                # Heartbeat timer — check elapsed since last tick COMPLETED
                now = asyncio.get_event_loop().time()
                if now - last_tick_time >= self._heartbeat_interval:
                    if not self.is_stopped() and not self._agent_lock.locked():
                        try:
                            await self.tick()
                        except Exception as exc:
                            self._append_context({"type": "error", "content": str(exc)})
                    # Reset timer AFTER tick completes (not before),
                    # so tick duration never cuts into the next interval.
                    last_tick_time = asyncio.get_event_loop().time()

                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            self._append_context({"type": "status", "value": "cancelled"})
            self._clear_pid()
            raise

        self._append_context({"type": "status", "value": "stopped"})
        self._clear_pid()

    # ── Properties ─────────────────────────────────────────────────

    @property
    def instance_dir(self) -> Path:
        return self._base_dir / self._instance_id

    @property
    def files_dir(self) -> Path:
        return self.instance_dir / "files"

    @property
    def kanban_path(self) -> Path:
        return self.instance_dir / "kanban.md"

    @property
    def _context_path(self) -> Path:
        return self.instance_dir / "context.jsonl"

    # ── Internal ───────────────────────────────────────────────────

    def _append_context(self, event: dict) -> None:
        """Append an event to context.jsonl (O(1), append-only)."""
        if self._ipc is not None:
            self._ipc.append(event)
        else:
            event.setdefault("ts", datetime.now().isoformat())
            with self._context_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
