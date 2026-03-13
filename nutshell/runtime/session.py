from __future__ import annotations
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from nutshell.core.agent import Agent
from nutshell.core.skill import Skill
from nutshell.core.tool import Tool
from nutshell.core.types import AgentResult
from nutshell.runtime.params import ensure_session_params, read_session_params, write_session_params
from nutshell.runtime.provider_factory import provider_name, resolve_provider
from nutshell.runtime.status import ensure_session_status, read_session_status, write_session_status

if TYPE_CHECKING:
    from nutshell.runtime.ipc import FileIPC

SESSIONS_DIR = Path("sessions")
DEFAULT_HEARTBEAT_INTERVAL = 600.0  # 10 minutes
SESSION_FINISHED = "SESSION_FINISHED"


class Session:
    """Agent persistent run context (server mode only).

    Disk layout: sessions/<id>/
        params.json      — model, provider, heartbeat_interval (source of truth; agent edits via bash)
        tasks.md         — free-form task notes (plain file read/write)
        files/           — associated files directory
        prompts/
            memory.md    — persistent memory (auto-injected each activation)
        skills/          — per-session skill .md files
        tools/           — reserved (v2)
        _system_log/
            manifest.json  — static config (entity, created_at)
            status.json    — all dynamic runtime state
            context.jsonl  — append-only log: user_input, turn
            events.jsonl   — runtime/UI events

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
        self.prompts_dir.mkdir(exist_ok=True)
        self.skills_dir.mkdir(exist_ok=True)
        self.tools_dir.mkdir(exist_ok=True)
        self.system_log_dir.mkdir(exist_ok=True)
        if not self.tasks_path.exists():
            self.tasks_path.write_text("", encoding="utf-8")
        if not self._context_path.exists():
            self._context_path.touch()
        if not self._events_path.exists():
            self._events_path.touch()
        if not self.memory_path.exists():
            self.memory_path.write_text("", encoding="utf-8")
        ensure_session_status(self.session_dir)
        ensure_session_params(self.session_dir, heartbeat_interval=heartbeat)

        # Baseline snapshot — _load_session_capabilities restores to these each activation
        self._entity_system_prompt: str = agent.system_prompt
        self._entity_skills: list[Skill] = list(agent.skills)
        self._entity_tools: list[Tool] = list(agent.tools)
        self._entity_model: str = agent.model

    # ── Capability loading ─────────────────────────────────────────

    def _load_session_capabilities(self) -> None:
        """Reload params, memory, and session skills. Call inside agent lock before each run."""
        from nutshell.runtime.loaders.skill import SkillLoader

        # 1. params.json → provider + model, mirror heartbeat_interval to status.json for UI
        params = read_session_params(self.session_dir)

        # Switch provider if params explicitly specifies one different from current
        desired_provider = (params.get("provider") or "").lower()
        if desired_provider and provider_name(self._agent._provider) != desired_provider:
            self._agent._provider = resolve_provider(desired_provider)

        # Apply model (null → entity baseline)
        self._agent.model = params.get("model") or self._entity_model

        # Write actual running values back so params.json always reflects reality
        actual_pname = provider_name(self._agent._provider)
        if actual_pname:  # skip write for unknown/mock providers (test safety)
            write_session_params(
                self.session_dir,
                provider=actual_pname,
                model=self._agent.model,
            )

        write_session_status(self.session_dir, heartbeat_interval=params["heartbeat_interval"])

        # 2. memory.md + session paths → rebuild system_prompt
        memory_content = self.memory_path.read_text(encoding="utf-8").strip()
        memory_block = f"\n\n---\n## Session Memory\n\n{memory_content}" if memory_content else ""
        self._agent.system_prompt = (
            self._entity_system_prompt
            + self._build_session_paths_block()
            + memory_block
        )

        # 3. session skills: merge (session overrides entity by name, new ones appended)
        session_skills: list[Skill] = []
        try:
            session_skills = SkillLoader().load_dir(self.skills_dir)
        except Exception:
            pass
        session_by_name = {s.name: s for s in session_skills}
        entity_names = {s.name for s in self._entity_skills}
        merged = [session_by_name.get(s.name, s) for s in self._entity_skills]
        merged += [s for s in session_skills if s.name not in entity_names]
        self._agent.skills = merged

        # 4. reset tools to entity baseline (prevents duplication across activations)
        self._agent.tools = list(self._entity_tools)

    def _build_session_paths_block(self) -> str:
        template = self._agent.session_context_template
        if not template:
            return ""
        return "\n\n---\n" + template.format(session_id=self._session_id)

    # ── History persistence ────────────────────────────────────────

    @staticmethod
    def _clean_content_for_api(content):
        """Strip storage-only fields from message content blocks.

        Older sessions stored extra fields (e.g. 'ts') inside content blocks.
        The Anthropic API rejects any unrecognised fields with a 400 error, so
        we allow-list the fields that are valid for each known block type and
        drop everything else.
        """
        if not isinstance(content, list):
            return content
        _ALLOWED: dict[str, set] = {
            "text":        {"type", "text"},
            "tool_use":    {"type", "id", "name", "input"},
            "tool_result": {"type", "tool_use_id", "content", "is_error"},
            "image":       {"type", "source"},
        }
        cleaned = []
        for block in content:
            if isinstance(block, dict):
                allowed = _ALLOWED.get(block.get("type", ""))
                cleaned.append(
                    {k: v for k, v in block.items() if k in allowed}
                    if allowed else dict(block)
                )
            else:
                cleaned.append(block)
        return cleaned

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
                                content = self._clean_content_for_api(m["content"])
                                history.append(Message(role=m["role"], content=content))
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
        tool_call_cb, get_tool_call_count = self._make_tool_call_callback()
        try:
            async with self._agent_lock:
                self._load_session_capabilities()
                result = await self._agent.run(
                    message,
                    on_text_chunk=self._make_text_chunk_callback(),
                    on_tool_call=tool_call_cb,
                )
        except BaseException:
            self._set_model_status("idle", "user")
            raise

        # Append full turn (the user_input event was already written by the UI
        # via send_message before the server picked it up).
        turn: dict = {
            "type": "turn",
            "triggered_by": "user",
            "messages": self._serialize_turn_messages(result.messages[old_len:]),
        }
        if get_tool_call_count() > 0:
            turn["has_streaming_tools"] = True
        self._append_context(turn)
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

        heartbeat_instructions = self._agent.heartbeat_prompt
        if heartbeat_instructions and "{tasks}" in heartbeat_instructions:
            prompt = heartbeat_instructions.format(tasks=tasks_content)
        else:
            prompt = f"Heartbeat activation.\n\nCurrent tasks:\n{tasks_content}"
            if heartbeat_instructions:
                prompt += f"\n\n{heartbeat_instructions}"

        # Write heartbeat_trigger event BEFORE starting so it appears in the UI
        # before the thinking bubble (not after the agent turn is complete)
        trigger_ts = datetime.now().isoformat()
        self._append_event({"type": "heartbeat_trigger", "ts": trigger_ts})
        self._set_model_status("running", "heartbeat")
        tool_call_cb, get_tool_call_count = self._make_tool_call_callback()
        try:
            async with self._agent_lock:
                self._load_session_capabilities()
                result = await self._agent.run(
                    prompt,
                    on_text_chunk=self._make_text_chunk_callback(),
                    on_tool_call=tool_call_cb,
                )
        except BaseException:
            self._set_model_status("idle", "heartbeat")
            raise

        if SESSION_FINISHED in result.content:
            # Clear tasks, prune heartbeat history so it doesn't pollute context
            self.tasks_path.write_text("", encoding="utf-8")
            self._agent._history = history_snapshot
            self._append_event({"type": "heartbeat_finished"})
        else:
            # Only log to context if session is still active — skip if user stopped
            # the session while this heartbeat was in-flight (avoids ghost output in UI)
            if not self.is_stopped():
                turn: dict = {
                    "type": "turn",
                    "triggered_by": "heartbeat",
                    "pre_triggered": True,  # heartbeat_trigger was pre-emitted
                    "trigger_ts": trigger_ts,
                    "messages": self._serialize_turn_messages(result.messages[old_len:]),
                }
                if get_tool_call_count() > 0:
                    turn["has_streaming_tools"] = True
                self._append_context(turn)

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
        input_offset = ipc.context_size()

        # Initialise heartbeat timer.
        # Use last_run_at from status.json so the interval is correctly preserved
        # across server restarts: if the agent ran 3m ago and interval is 10m,
        # the next heartbeat fires in 7m, not 10m from now.
        # Cap elapsed time at current_interval so we never fire immediately on startup
        # (this handles the case where the server was down longer than one interval).
        _now_mono = asyncio.get_event_loop().time()
        _st = read_session_status(self.session_dir)
        _last_run_str = _st.get("last_run_at")
        _init_interval = float(
            read_session_params(self.session_dir).get("heartbeat_interval")
            or self._heartbeat_interval
        )
        if _last_run_str:
            try:
                _elapsed = (datetime.now() - datetime.fromisoformat(_last_run_str)).total_seconds()
                # Clamp: don't go further back than one full interval
                last_tick_time = _now_mono - min(_elapsed, _init_interval)
            except Exception:
                last_tick_time = _now_mono
        else:
            last_tick_time = _now_mono

        try:
            while True:
                # Poll for new user_input events
                inputs, input_offset = ipc.poll_inputs(input_offset)
                for msg in inputs:
                    content = msg.get("content", "")
                    # User message wakes a stopped session
                    if self.is_stopped():
                        self.set_status("active")
                        self._append_event({"type": "status", "value": "resumed"})
                    # Context reshape: clean up any orphaned user message at history tail
                    # (e.g., a heartbeat prompt interrupted mid-run)
                    content = self._reshape_history(content)
                    try:
                        await self.chat(content)
                    except Exception as exc:
                        self._append_event({"type": "error", "content": str(exc)})
                    finally:
                        # Reset heartbeat timer after every agent run (user-triggered).
                        # The timer is inherently blocked during the await above (single
                        # event loop), but resetting here ensures the full interval elapses
                        # from the moment output completes, not from when the message arrived.
                        last_tick_time = asyncio.get_event_loop().time()

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
                                self._append_event({"type": "status", "value": "auto-expired after 5h stopped"})
                        except Exception:
                            pass

                # Heartbeat timer — read interval fresh from params.json each cycle
                # so edits to params.json take effect without restarting the daemon.
                now = asyncio.get_event_loop().time()
                current_interval = float(
                    read_session_params(self.session_dir).get("heartbeat_interval")
                    or self._heartbeat_interval
                )
                if now - last_tick_time >= current_interval:
                    if not self.is_stopped() and not self._agent_lock.locked():
                        try:
                            await self.tick()
                        except Exception as exc:
                            self._append_event({"type": "error", "content": str(exc)})
                    # Reset timer AFTER tick completes (not before),
                    # so tick duration never cuts into the next interval.
                    last_tick_time = asyncio.get_event_loop().time()

                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            self._set_model_status("idle", "system")
            self._append_event({"type": "status", "value": "cancelled"})
            self._clear_pid()
            raise

        self._set_model_status("idle", "system")
        self._append_event({"type": "status", "value": "stopped"})
        self._clear_pid()

    # ── Properties ─────────────────────────────────────────────────

    @property
    def session_dir(self) -> Path:
        return self._base_dir / self._session_id

    @property
    def files_dir(self) -> Path:
        return self.session_dir / "files"

    @property
    def prompts_dir(self) -> Path:
        return self.session_dir / "prompts"

    @property
    def skills_dir(self) -> Path:
        return self.session_dir / "skills"

    @property
    def tools_dir(self) -> Path:
        return self.session_dir / "tools"

    @property
    def system_log_dir(self) -> Path:
        return self.session_dir / "_system_log"

    @property
    def memory_path(self) -> Path:
        return self.session_dir / "prompts" / "memory.md"

    @property
    def tasks_path(self) -> Path:
        return self.session_dir / "tasks.md"

    @property
    def _context_path(self) -> Path:
        return self.session_dir / "_system_log" / "context.jsonl"

    @property
    def _events_path(self) -> Path:
        return self.session_dir / "_system_log" / "events.jsonl"

    # ── Internal ───────────────────────────────────────────────────

    def _append_context(self, event: dict) -> None:
        """Append a conversation event (user_input or turn) to context.jsonl."""
        if self._ipc is not None:
            self._ipc.append_context(event)
        else:
            event.setdefault("ts", datetime.now().isoformat())
            with self._context_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _append_event(self, event: dict) -> None:
        """Append a runtime/UI event to events.jsonl."""
        if self._ipc is not None:
            self._ipc.append_event(event)
        else:
            event.setdefault("ts", datetime.now().isoformat())
            with self._events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _set_model_status(self, state: str, source: str) -> str:
        ts = datetime.now().isoformat()
        self._append_event({"type": "model_status", "state": state, "source": source, "ts": ts})
        updates: dict = {"model_state": state, "model_source": source}
        if state == "idle":
            updates["last_run_at"] = ts
        write_session_status(self.session_dir, **updates)
        return ts

    def _make_tool_call_callback(self):
        """Return (callback, counter) pair for streaming tool call events.

        The callback writes a tool_call event to context.jsonl for each tool
        invoked, giving the UI real-time visibility before results return.
        The counter reports how many tool calls were streamed (used to mark
        the turn with has_streaming_tools=True so history doesn't duplicate them).
        """
        count: list[int] = [0]

        def on_tool_call(name: str, input: dict) -> None:
            count[0] += 1
            self._append_event({"type": "tool_call", "name": name, "input": input})

        def get_count() -> int:
            return count[0]

        return on_tool_call, get_count

    def _reshape_history(self, new_content: str) -> str:
        """Clean up orphaned trailing user message before processing new user input.

        If the agent history ends with an unresponded user message (e.g., a
        heartbeat prompt interrupted mid-run), we either drop it (if it was a
        heartbeat prompt) or merge it with the new message (if it was a real
        user message), to prevent consecutive user messages which the API rejects.
        """
        if not self._agent._history or self._agent._history[-1].role != "user":
            return new_content
        last = self._agent._history[-1]
        last_content = last.content if isinstance(last.content, str) else ""
        self._agent._history.pop()
        if "Heartbeat activation" in last_content:
            # Orphaned heartbeat prompt — drop it, use new message as-is
            return new_content
        # Orphaned real user message — merge with new input
        return f"{last_content}\n\n{new_content}"

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
                self._append_event({"type": "partial_text", "content": accumulated})
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
        # Return plain dict copies. Do NOT add extra fields (e.g. ts) that the
        # Anthropic API rejects when these blocks are loaded back into history.
        return [dict(block) if isinstance(block, dict) else block for block in content]
