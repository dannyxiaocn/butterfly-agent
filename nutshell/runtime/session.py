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
from nutshell.runtime.status import ensure_session_status, read_session_status, write_session_status

if TYPE_CHECKING:
    from nutshell.runtime.ipc import FileIPC

SESSIONS_DIR = Path("sessions")
DEFAULT_HEARTBEAT_INTERVAL = 10.0  # seconds
SESSION_FINISHED = "SESSION_FINISHED"


class Session:
    """Agent persistent run context (server mode only).

    Disk layout: sessions/<id>/
        manifest.json    — config + runtime state (entity, heartbeat, status, pid)
        tasks.md         — free-form task notes (plain file read/write)
        context.jsonl    — append-only log: user_input, turn, status, error, heartbeat_finished
        files/           — associated files directory

    Usage:
        session = Session(agent, session_id="my-project")
        ipc     = FileIPC(session.session_dir)
        await session.run_daemon_loop(ipc)

    Resuming an existing session uses the same constructor — directory
    creation is idempotent (existing files are never overwritten).
    """

    def __init__(
        self,
        agent: Agent,
        session_id: str | None = None,
        base_dir: Path = SESSIONS_DIR,
        heartbeat: float = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        self._agent = agent
        self._session_id = session_id or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._base_dir = base_dir
        self._heartbeat_interval = heartbeat
        self._agent_lock: asyncio.Lock = asyncio.Lock()
        self._ipc: FileIPC | None = None

        # Idempotent directory creation — safe for both new and resumed sessions
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(exist_ok=True)
        if not self.tasks_path.exists():
            self.tasks_path.write_text("", encoding="utf-8")
        if not self._context_path.exists():
            self._context_path.touch()
        ensure_session_status(self.session_dir)

        self._inject_task_tools(agent)

    def _inject_task_tools(self, agent: Agent) -> None:
        tasks_path = self.tasks_path

        @tool(description="Read the current task list")
        def read_tasks() -> str:
            content = tasks_path.read_text(encoding="utf-8").strip()
            return content or "(empty)"

        @tool(description="Overwrite the task list. Pass empty string to clear all tasks.")
        def write_tasks(content: str) -> str:
            tasks_path.write_text(content, encoding="utf-8")
            return "Tasks updated."

        agent.tools.extend([read_tasks, write_tasks])

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
        self._set_model_status("running", "user")
        try:
            async with self._agent_lock:
                result = await self._agent.run(
                    message,
                    on_text_chunk=self._make_text_chunk_callback(),
                )
        except BaseException:
            self._set_model_status("idle", "user")
            raise

        # Append full turn (the user_input event was already written by the UI
        # via send_message before the server picked it up).
        self._append_context({
            "type": "turn",
            "triggered_by": "user",
            "messages": self._serialize_turn_messages(result.messages[old_len:]),
        })
        self._set_model_status("idle", "user")
        return result

    async def tick(self) -> AgentResult | None:
        """Single heartbeat: run agent if tasks are non-empty.

        Returns None if tasks are empty.
        Clears tasks and prunes history if agent responds SESSION_FINISHED.
        """
        tasks_content = self.tasks_path.read_text(encoding="utf-8").strip()
        if not tasks_content:
            return None

        # Snapshot history so we can roll back if SESSION_FINISHED
        history_snapshot = list(self._agent._history)
        old_len = len(self._agent._history)

        heartbeat_instructions = self._agent.heartbeat_prompt or "Continue working on your tasks."
        prompt = f"Heartbeat activation.\n\nCurrent tasks:\n{tasks_content}\n\n{heartbeat_instructions}"

        trigger_ts = self._set_model_status("running", "heartbeat")
        try:
            async with self._agent_lock:
                result = await self._agent.run(
                    prompt,
                    on_text_chunk=self._make_text_chunk_callback(),
                )
        except BaseException:
            self._set_model_status("idle", "heartbeat")
            raise

        if SESSION_FINISHED in result.content:
            # Clear tasks, prune heartbeat history so it doesn't pollute context
            self.tasks_path.write_text("", encoding="utf-8")
            self._agent._history = history_snapshot
            self._append_context({"type": "heartbeat_finished"})
        else:
            # Only log to context if session is still active — skip if user stopped
            # the session while this heartbeat was in-flight (avoids ghost output in UI)
            if not self.is_stopped():
                self._append_context({
                    "type": "turn",
                    "triggered_by": "heartbeat",
                    "trigger_ts": trigger_ts,
                    "messages": self._serialize_turn_messages(result.messages[old_len:]),
                })

        self._set_model_status("idle", "heartbeat")
        return result

    # ── Stop / Start ───────────────────────────────────────────────

    def is_stopped(self) -> bool:
        """True if status.json has status=stopped."""
        return read_session_status(self.session_dir).get("status") == "stopped"

    def set_status(self, status: str) -> None:
        """Write status field to status.json. Clears stopped_at when resuming."""
        updates: dict = {"status": status}
        if status == "active":
            updates["stopped_at"] = None
        write_session_status(self.session_dir, **updates)

    def _write_pid(self) -> None:
        """Write current process PID into status.json."""
        write_session_status(self.session_dir, pid=os.getpid())

    def _clear_pid(self) -> None:
        """Clear PID from status.json when daemon stops."""
        write_session_status(self.session_dir, pid=None)

    # ── Server loop ────────────────────────────────────────────────

    async def run_daemon_loop(self, ipc: "FileIPC") -> None:
        """Run as a server-managed session.

        Polls context.jsonl for user_input events every 0.5s.
        Fires heartbeat ticks every heartbeat_interval seconds.

        Heartbeat is skipped when:
          - session status == "stopped" (user issued /stop)
          - agent_lock is held (agent already running)

        A user message always wakes a stopped session (clears stopped status).
        last_tick_time is updated AFTER the tick completes, so tick duration
        never eats into the next interval.
        """
        self._ipc = ipc
        self._write_pid()
        # Reset stale "running" state from a previous crash
        write_session_status(self.session_dir, model_state="idle", model_source="system")

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
                    # User message wakes a stopped session
                    if self.is_stopped():
                        self.set_status("active")
                        self._append_context({"type": "status", "value": "resumed"})
                    try:
                        await self.chat(content)
                    except Exception as exc:
                        self._append_context({"type": "error", "content": str(exc)})

                # Auto-expire stopped sessions after 5 hours
                if self.is_stopped():
                    st = read_session_status(self.session_dir)
                    stopped_at_str = st.get("stopped_at")
                    if stopped_at_str:
                        try:
                            elapsed = (datetime.now() - datetime.fromisoformat(stopped_at_str)).total_seconds()
                            if elapsed >= 5 * 3600:
                                self.tasks_path.write_text("", encoding="utf-8")
                                write_session_status(self.session_dir, status="active", stopped_at=None)
                                self._append_context({"type": "status", "value": "auto-expired after 5h stopped"})
                        except Exception:
                            pass

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
            self._set_model_status("idle", "system")
            self._append_context({"type": "status", "value": "cancelled"})
            self._clear_pid()
            raise

        self._set_model_status("idle", "system")
        self._append_context({"type": "status", "value": "stopped"})
        self._clear_pid()

    # ── Properties ─────────────────────────────────────────────────

    @property
    def session_dir(self) -> Path:
        return self._base_dir / self._session_id

    @property
    def files_dir(self) -> Path:
        return self.session_dir / "files"

    @property
    def tasks_path(self) -> Path:
        return self.session_dir / "tasks.md"

    @property
    def _context_path(self) -> Path:
        return self.session_dir / "context.jsonl"

    # ── Internal ───────────────────────────────────────────────────

    def _append_context(self, event: dict) -> None:
        """Append an event to context.jsonl (O(1), append-only)."""
        if self._ipc is not None:
            self._ipc.append(event)
        else:
            event.setdefault("ts", datetime.now().isoformat())
            with self._context_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _set_model_status(self, state: str, source: str) -> str:
        ts = datetime.now().isoformat()
        self._append_context({"type": "model_status", "state": state, "source": source, "ts": ts})
        updates: dict = {"model_state": state, "model_source": source}
        if state == "idle":
            updates["last_run_at"] = ts
        write_session_status(self.session_dir, **updates)
        return ts

    def _make_text_chunk_callback(self):
        """Return a sync callback that writes throttled partial_text events.

        Chunks are buffered and flushed every ~150 characters to limit
        write frequency while still giving the UI near-real-time feedback.
        """
        buf: list[str] = []
        buf_len: list[int] = [0]
        FLUSH_THRESHOLD = 150

        def on_chunk(chunk: str) -> None:
            buf.append(chunk)
            buf_len[0] += len(chunk)
            if buf_len[0] >= FLUSH_THRESHOLD:
                accumulated = "".join(buf)
                self._append_context({"type": "partial_text", "content": accumulated})
                buf.clear()
                buf_len[0] = 0

        return on_chunk

    def _serialize_turn_messages(self, messages: list) -> list[dict]:
        serialized: list[dict] = []
        for message in messages:
            entry = {
                "role": message.role,
                "ts": datetime.now().isoformat(),
                "content": self._serialize_message_content(message.content),
            }
            serialized.append(entry)
        return serialized

    def _serialize_message_content(self, content):
        if not isinstance(content, list):
            return content
        blocks: list = []
        for block in content:
            if isinstance(block, dict):
                payload = dict(block)
                payload.setdefault("ts", datetime.now().isoformat())
                blocks.append(payload)
            else:
                blocks.append(block)
        return blocks
