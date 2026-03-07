from __future__ import annotations
import asyncio
import json
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
        kanban.md        — free-form task notes (plain file read/write)
        context.json     — pure IO log: user / agent / tool events
        .nutshell_log    — system operations log (JSONL, append-only)
        inbox.jsonl      — UI → server
        outbox.jsonl     — server → UI
        daemon.pid       — server PID
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
            self._context_path.write_text("[]", encoding="utf-8")

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

    # ── Activation ────────────────────────────────────────────────

    async def chat(self, message: str, reply_to: str | None = None) -> AgentResult:
        """Run agent with user message. Holds agent lock — blocks heartbeat tick."""
        self._append_context({"type": "user", "content": message})
        async with self._agent_lock:
            result = await self._agent.run(message)
        self._append_context({"type": "agent", "content": result.content})
        for tc in result.tool_calls:
            self._append_context({"type": "tool", "name": tc.name, "input": tc.input})

        if self._ipc is not None:
            for tc in result.tool_calls:
                self._ipc.append_outbox({"type": "tool", "name": tc.name, "input": tc.input})
            agent_event: dict = {"type": "agent", "content": result.content}
            if reply_to:
                agent_event["reply_to"] = reply_to
            self._ipc.append_outbox(agent_event)

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

        prompt = (
            f"Check your kanban and continue working.\n\n"
            f"--- Kanban ---\n{kanban_content}\n---\n\n"
            f"When you finish ALL tasks, you MUST call write_kanban(\"\") to clear the board. "
            f"This is the only way to signal completion. Do not just say tasks are done — "
            f"you must actually call write_kanban(\"\").\n\n"
            f"If all work is done and there is nothing remaining, respond with exactly: {INSTANCE_FINISHED}\n"
            f"This will clear the kanban and end this instance."
        )

        self._syslog({"event": "heartbeat_triggered", "interval": self._heartbeat_interval})
        async with self._agent_lock:
            result = await self._agent.run(prompt)

        if INSTANCE_FINISHED in result.content:
            # Clear kanban, prune heartbeat history so it doesn't pollute context
            self.kanban_path.write_text("", encoding="utf-8")
            self._agent._history = history_snapshot
            self._syslog({"event": "heartbeat_finished", "reason": INSTANCE_FINISHED})
            if self._ipc is not None:
                self._ipc.append_outbox({"type": "heartbeat_finished"})
        else:
            if self._ipc is not None and result.content:
                self._ipc.append_outbox({"type": "heartbeat", "content": result.content})

        return result

    # ── Server loop ────────────────────────────────────────────────

    async def run_daemon_loop(self, ipc: "FileIPC") -> None:
        """Run as a server-managed instance.

        Polls inbox.jsonl for user messages, fires heartbeat ticks,
        and writes all output to outbox.jsonl. Blocks until done or cancelled.
        """
        self._ipc = ipc
        ipc.write_pid()
        self._syslog({"event": "instance_created", "id": self._instance_id})

        inbox_offset = 0
        last_tick_time = asyncio.get_event_loop().time()

        try:
            while True:
                # Poll inbox
                msgs, inbox_offset = ipc.poll_inbox(inbox_offset)
                for msg in msgs:
                    if msg.get("type") == "user":
                        content = msg.get("content", "")
                        msg_id = msg.get("id")
                        self._syslog({"event": "user_message", "id": msg_id})
                        try:
                            await self.chat(content, reply_to=msg_id)
                        except Exception as exc:
                            ipc.append_outbox({"type": "error", "content": str(exc)})

                # Heartbeat timer
                now = asyncio.get_event_loop().time()
                if now - last_tick_time >= self._heartbeat_interval:
                    last_tick_time = now
                    if self._agent_lock.locked():
                        self._syslog({"event": "heartbeat_skipped", "reason": "agent_busy"})
                    else:
                        try:
                            await self.tick()
                        except Exception as exc:
                            self._syslog({"event": "heartbeat_error", "error": str(exc)})

                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            ipc.append_outbox({"type": "status", "value": "cancelled"})
            ipc.clear_pid()
            self._syslog({"event": "instance_closed", "status": "cancelled"})
            raise

        ipc.append_outbox({"type": "status", "value": "stopped"})
        ipc.clear_pid()
        self._syslog({"event": "instance_closed", "status": "done"})

    # ── Status ─────────────────────────────────────────────────────

    def is_done(self) -> bool:
        """True when kanban is empty."""
        return not self.kanban_path.read_text(encoding="utf-8").strip()

    def close(self, status: str = "done") -> None:
        """Write final status event to context.json."""
        self._append_context({"type": "status", "value": status})

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
        return self.instance_dir / "context.json"

    @property
    def _syslog_path(self) -> Path:
        return self.instance_dir / ".nutshell_log"

    # ── Internal ───────────────────────────────────────────────────

    def _append_context(self, event: dict) -> None:
        """Append to context.json (pure IO log: user/agent/tool/status)."""
        event.setdefault("ts", datetime.now().isoformat())
        events: list = json.loads(self._context_path.read_text(encoding="utf-8"))
        events.append(event)
        self._context_path.write_text(
            json.dumps(events, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _syslog(self, event: dict) -> None:
        """Append to .nutshell_log (system operations, JSONL)."""
        event.setdefault("ts", datetime.now().isoformat())
        with self._syslog_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
