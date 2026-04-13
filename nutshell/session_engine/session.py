from __future__ import annotations
import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from nutshell.core.agent import Agent
from nutshell.core.hook import OnLoopEnd, OnLoopStart, OnTextChunk, OnToolCall, OnToolDone
from nutshell.core.tool import Tool
from nutshell.core.types import AgentResult
from nutshell.session_engine.session_config import read_config, ensure_config
from nutshell.session_engine.task_cards import (
    TaskCard, clear_all_cards,
    load_due_cards, migrate_legacy_task_sources, save_card,
)
from nutshell.llm_engine.registry import provider_name, resolve_provider
from nutshell.session_engine.session_status import ensure_session_status, read_session_status, write_session_status
from nutshell.tool_engine.loader import ToolLoader

if TYPE_CHECKING:
    from nutshell.runtime.ipc import FileIPC

SESSIONS_DIR = Path(__file__).parent.parent.parent / "sessions"
_SYSTEM_SESSIONS_DIR = Path(__file__).parent.parent.parent / "_sessions"
SESSION_FINISHED = "SESSION_FINISHED"


class Session:
    """Agent persistent run context (server mode only).

    Disk layout:
        sessions/<id>/                ← agent-visible
          core/
            system.md               ← system prompt (copied from entity at creation)
            task.md                 ← task wakeup prompt (fallback: heartbeat.md)
            env.md                  ← session paths + operational guide (fallback: session.md)
            memory.md               ← persistent memory (auto-injected each activation)
            tasks/*.md              ← task cards (YAML frontmatter + content)
            params.json             ← runtime config
            tools.md                ← enabled toolhub tools (one name per line)
            skills.md               ← enabled skillhub skills (one name per line)
            tools/                  ← agent-created tools: .json + .sh
            skills/                 ← agent-created skills
          docs/                     ← user-uploaded files
          playground/               ← agent's free workspace

        _sessions/<id>/             ← system-only twin (agent never sees this)
          manifest.json             ← static: entity name, created_at
          status.json               ← dynamic runtime state
          context.jsonl             ← conversation history
          events.jsonl              ← runtime/UI events

    Usage:
        session = Session(agent, session_id="my-project")
        ipc     = FileIPC(session.system_dir)
        await session.run_daemon_loop(ipc)

    Resuming an existing session uses the same constructor — directory
    creation is idempotent (existing files are never overwritten).
    """

    def __init__(
        self,
        agent: Agent,
        session_id: str | None = None,
        base_dir: Path = SESSIONS_DIR,
        system_base: Path = _SYSTEM_SESSIONS_DIR,
        heartbeat: float = 600.0,  # legacy param, ignored
        *,
        on_loop_start: OnLoopStart | None = None,
        on_loop_end: OnLoopEnd | None = None,
        on_tool_done: OnToolDone | None = None,
        on_tool_call: OnToolCall | None = None,
        on_text_chunk: OnTextChunk | None = None,
    ) -> None:
        self._agent = agent
        self._session_id = session_id or (datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "-" + uuid.uuid4().hex[:4])
        self._base_dir = base_dir
        self._system_base = system_base
        # heartbeat param accepted for backward compat but ignored
        self._agent_lock: asyncio.Lock = asyncio.Lock()
        self._ipc: FileIPC | None = None

        # External hooks — composed with internal IPC callbacks in chat()/tick()
        self.on_loop_start = on_loop_start
        self.on_loop_end = on_loop_end
        self.on_tool_done = on_tool_done
        self.on_tool_call = on_tool_call
        self.on_text_chunk = on_text_chunk

        # Idempotent directory creation — safe for both new and resumed sessions
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.core_dir.mkdir(exist_ok=True)
        (self.core_dir / "tools").mkdir(exist_ok=True)
        (self.core_dir / "skills").mkdir(exist_ok=True)
        self.docs_dir.mkdir(exist_ok=True)
        self.playground_dir.mkdir(exist_ok=True)
        self.system_dir.mkdir(parents=True, exist_ok=True)
        # Migrate legacy task sources into core/tasks/
        migrate_legacy_task_sources(self.session_dir)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        if not self.memory_path.exists():
            self.memory_path.write_text("", encoding="utf-8")
        if not self._context_path.exists():
            self._context_path.touch()
        if not self._events_path.exists():
            self._events_path.touch()
        ensure_session_status(self.system_dir)
        ensure_config(self.session_dir)

    # ── Capability loading ─────────────────────────────────────────

    def _read_core_text(self, name: str) -> str:
        """Read a file from core/ returning empty string if missing."""
        p = self.core_dir / name
        try:
            return p.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, PermissionError):
            return ""

    def _load_session_capabilities(self) -> None:
        """Reload params, prompts, skills, and tools from core/. Call inside agent lock before each run."""
        from nutshell.skill_engine.loader import SkillLoader

        # 1. config → provider + model
        cfg = read_config(self.session_dir)

        desired_provider = (cfg.get("provider") or "").lower()
        if desired_provider and provider_name(self._agent._provider) != desired_provider:
            self._agent._provider = resolve_provider(desired_provider)

        self._agent.model = cfg.get("model") or self._agent.model
        self._agent.thinking = bool(cfg.get("thinking", self._agent.thinking))
        self._agent.thinking_budget = int(cfg.get("thinking_budget", self._agent.thinking_budget))
        if cfg.get("thinking_effort"):
            self._agent.thinking_effort = str(cfg["thinking_effort"])
        if cfg.get("fallback_model"):
            self._agent.fallback_model = cfg["fallback_model"]
        if cfg.get("fallback_provider"):
            self._agent._fallback_provider_str = cfg["fallback_provider"]
            self._agent._fallback_provider = None  # reset so it re-resolves on next use

        # 2. prompts from core/
        system_md = self._read_core_text("system.md")
        # env.md is the canonical name; fall back to session.md / session_context.md for old sessions
        env_md = self._read_core_text("env.md") or self._read_core_text("session.md") or self._read_core_text("session_context.md")

        self._agent.system_prompt = system_md
        self._agent.env_context = (
            env_md.replace("{session_id}", self._session_id) if env_md else ""
        )
        # task.md is the canonical name; fall back to heartbeat.md for old sessions
        self._agent.task_prompt = self._read_core_text("task.md") or self._read_core_text("heartbeat.md")
        self._agent.memory = self.memory_path.read_text(encoding="utf-8").strip()

        # Extra named memory layers from core/memory/*.md (sorted, non-empty only)
        memory_dir = self.core_dir / "memory"
        extra_layers: list[tuple[str, str]] = []
        if memory_dir.is_dir():
            for md_file in sorted(memory_dir.glob("*.md")):
                content = md_file.read_text(encoding="utf-8").strip()
                if content:
                    extra_layers.append((md_file.stem, content))
        self._agent.memory_layers = extra_layers

        # App notifications from core/apps/*.md (sorted, non-empty only)
        apps_dir = self.core_dir / "apps"
        app_notifications: list[tuple[str, str]] = []
        if apps_dir.is_dir():
            for md_file in sorted(apps_dir.glob("*.md")):
                content = md_file.read_text(encoding="utf-8").strip()
                if content:
                    app_notifications.append((md_file.stem, content))
        self._agent.app_notifications = app_notifications

        # 3. skills from skills.md (skillhub) + local skills from core/skills/
        try:
            loader = SkillLoader()
            skills_md_path = self.core_dir / "skills.md"
            if skills_md_path.exists():
                skills = loader.load_from_skills_md(skills_md_path)
                # Also load agent-created skills from core/skills/
                skills_dir = self.core_dir / "skills"
                if skills_dir.is_dir():
                    skills.extend(loader.load_dir(skills_dir))
            else:
                # Fallback: load all from core/skills/ directory
                skills = loader.load_dir(self.core_dir / "skills")
        except (FileNotFoundError, PermissionError):
            skills = []
        except Exception as e:
            print(f"[session] Warning: failed to load skills: {e}")
            skills = []
        self._agent.skills = skills

        # 4. tools from tools.md (toolhub) + local tools from core/tools/
        # default_workdir: bash and shell tools run from the session directory so
        # agents use short relative paths (core/tasks/) instead of full session paths.
        try:
            loader = ToolLoader(
                default_workdir=str(self.session_dir),
                skills=skills,
                tasks_dir=self.tasks_dir,
                memory_dir=self.core_dir / "memory",
            )
            # Load tools from tools.md (toolhub)
            tools_md_path = self.core_dir / "tools.md"
            if tools_md_path.exists():
                tools = loader.load_from_tool_md(tools_md_path)
                # Also load agent-created tools from core/tools/ (.json+.sh pairs)
                tools.extend(loader.load_local_tools(self.core_dir / "tools"))
            else:
                # Legacy fallback: load from core/tools/*.json (handles .sh too)
                tools = loader.load_dir(self.core_dir / "tools")
        except (FileNotFoundError, PermissionError):
            tools = []
        except Exception as e:
            print(f"[session] Warning: failed to load tools: {e}")
            tools = []

        # Apply tool_providers overrides (e.g. web_search → brave/tavily)
        tool_providers = cfg.get("tool_providers") or {}
        if tool_providers:
            from nutshell.tool_engine.registry import resolve_tool_impl
            for i, t in enumerate(tools):
                if t.name in tool_providers:
                    tool_provider_key = tool_providers[t.name]
                    impl = resolve_tool_impl(t.name, tool_provider_key)
                    if impl:
                        tools[i] = Tool(name=t.name, description=t.description, func=impl, schema=t.schema)

        # Inject reload_capabilities — always present, cannot be overridden from disk
        from nutshell.tool_engine.reload import create_reload_tool
        reload_tool = create_reload_tool(self)
        tools = [t for t in tools if t.name != "reload_capabilities"]
        tools.append(reload_tool)

        self._agent.tools = tools

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
                                raw_content = m.get("content")
                                if raw_content is None:
                                    continue
                                content = self._clean_content_for_api(raw_content)
                                history.append(Message(role=m["role"], content=content))
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass
        self._agent._history = history

    # ── Activation ────────────────────────────────────────────────

    def _expand_slash_command(self, message: str) -> str:
        """If message starts with /skill-name, inject full skill content as context."""
        if not message.startswith("/"):
            return message
        parts = message[1:].split(None, 1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        for skill in self._agent.skills:
            if skill.name == cmd:
                if skill.location is not None:
                    from nutshell.skill_engine.loader import _parse_frontmatter
                    text = Path(skill.location).read_text(encoding="utf-8")
                    _, body = _parse_frontmatter(text)
                else:
                    body = skill.body
                header = f"[Skill: {skill.name}]\n\n{body.strip()}"
                return f"{header}\n\n---\n\n{args}" if args else header
        return message

    async def chat(self, message: str, *, user_input_id: str | None = None, caller_type: str = "human") -> AgentResult:
        """Run agent with user message. Holds agent lock — blocks task tick.

        Args:
            caller_type: "human" or "agent" — passed to Agent.run() for prompt adaptation.
        """
        message = self._expand_slash_command(message)
        old_len = len(self._agent._history)
        self._set_model_status("running", "user")
        tool_call_cb, get_tool_call_count = self._make_tool_call_callback()
        on_chunk = self._make_text_chunk_callback()
        try:
            async with self._agent_lock:
                self._load_session_capabilities()
                result = await self._agent.run(
                    message,
                    on_text_chunk=on_chunk,
                    on_tool_call=tool_call_cb,
                    on_tool_done=self._make_tool_done_callback(),
                    on_loop_start=self._make_loop_start_callback(),
                    on_loop_end=self._make_loop_end_callback(),
                    caller_type=caller_type,
                )
        except BaseException:
            self._set_model_status("idle", "user")
            raise
        finally:
            on_chunk.flush()

        # Append full turn (the user_input event was already written by the UI
        # via send_message before the server picked it up).
        turn: dict = {
            "type": "turn",
            "triggered_by": "user",
            "messages": self._serialize_turn_messages(result.messages[old_len:]),
        }
        if user_input_id:
            turn["user_input_id"] = user_input_id
        if get_tool_call_count() > 0:
            turn["has_streaming_tools"] = True
        if result.usage and result.usage.total_tokens > 0:
            turn["usage"] = result.usage.as_dict()
        self._append_context(turn)
        self._set_model_status("idle", "user")
        return result

    async def tick(self, card: TaskCard | None = None) -> AgentResult | None:
        """Execute a single task card (or the next due card).

        If no card is provided, picks the first due card from core/tasks/.
        Returns None if no card is due.
        """
        migrate_legacy_task_sources(self.session_dir)
        if card is None:
            due = load_due_cards(self.tasks_dir)
            if due:
                card = due[0]
            else:
                return None

        triggered_by = f"task:{card.name}"
        task_info = card.description or card.name

        # Snapshot history so we can roll back if SESSION_FINISHED
        history_snapshot = list(self._agent._history)
        old_len = len(self._agent._history)

        # Build wakeup prompt from task.md template + task info
        task_prompt = self._agent.task_prompt
        if task_prompt and "{task}" in task_prompt:
            prompt = task_prompt.format(task=task_info)
        else:
            prompt = f"Task wakeup: {card.name}\n\n{task_info}"
            if task_prompt:
                prompt += f"\n\n{task_prompt}"

        trigger_ts = datetime.now().isoformat()
        self._append_event({"type": "task_wakeup", "card": card.name, "ts": trigger_ts})
        card.mark_working()
        save_card(self.tasks_dir, card)
        self._set_model_status("running", triggered_by)
        tool_call_cb, get_tool_call_count = self._make_tool_call_callback()
        on_chunk = self._make_text_chunk_callback()
        try:
            async with self._agent_lock:
                self._load_session_capabilities()
                result = await self._agent.run(
                    prompt,
                    on_text_chunk=on_chunk,
                    on_tool_call=tool_call_cb,
                    on_tool_done=self._make_tool_done_callback(),
                    on_loop_start=self._make_loop_start_callback(),
                    on_loop_end=self._make_loop_end_callback(),
                )
        except BaseException:
            card.mark_pending()
            save_card(self.tasks_dir, card)
            self._set_model_status("idle", triggered_by)
            raise
        finally:
            on_chunk.flush()

        if SESSION_FINISHED in result.content:
            clear_all_cards(self.tasks_dir)
            self._agent._history = history_snapshot
            self._append_event({"type": "task_finished", "card": card.name, "ts": trigger_ts})
        else:
            card.mark_finished()
            save_card(self.tasks_dir, card)

            # Replace verbose task prompt in history with a compact marker
            new_msgs = self._agent._history[old_len:]
            if new_msgs and new_msgs[0].role == "user":
                from nutshell.core.types import Message as _Msg
                marker = f"[Task:{card.name} {trigger_ts}]"
                new_msgs = [_Msg(role="user", content=marker), *new_msgs[1:]]
                self._agent._history = history_snapshot + new_msgs

            if not self.is_stopped():
                turn: dict = {
                    "type": "turn",
                    "triggered_by": triggered_by,
                    "trigger_ts": trigger_ts,
                    "messages": self._serialize_turn_messages(result.messages[old_len:]),
                }
                turn["pre_triggered"] = True
                if get_tool_call_count() > 0:
                    turn["has_streaming_tools"] = True
                if result.usage and result.usage.total_tokens > 0:
                    turn["usage"] = result.usage.as_dict()
                self._append_context(turn)

        self._set_model_status("idle", triggered_by)
        return result

    # ── Stop / Start ───────────────────────────────────────────────

    def is_stopped(self) -> bool:
        """True if status.json has status=stopped."""
        return read_session_status(self.system_dir).get("status") == "stopped"

    def set_status(self, status: str) -> None:
        """Write status field to status.json. Clears stopped_at when resuming."""
        updates: dict = {"status": status}
        if status == "active":
            updates["stopped_at"] = None
        write_session_status(self.system_dir, **updates)

    def _write_pid(self) -> None:
        """Write current process PID into status.json."""
        write_session_status(self.system_dir, pid=os.getpid())

    def _clear_pid(self) -> None:
        """Clear PID from status.json when daemon stops. Release git master claims."""
        write_session_status(self.system_dir, pid=None)
        # Release any git master registrations held by this session
        try:
            from nutshell.runtime.git_coordinator import GitCoordinator
            coordinator = GitCoordinator(system_base=self._system_base)
            coordinator.release(self._session_id)
        except Exception:
            pass  # best-effort cleanup

    # ── Server loop ────────────────────────────────────────────────

    async def run_daemon_loop(self, ipc: "FileIPC", stop_event: asyncio.Event | None = None) -> None:
        """Run as a server-managed session.

        Polls context.jsonl for user_input events every 0.5s.
        Checks task cards in core/tasks/ each cycle and runs any that are due.

        Task cards are skipped when:
          - session status == "stopped" (user issued /stop)
          - agent_lock is held (agent already running)

        A user message always wakes a stopped session (clears stopped status).
        """
        self._ipc = ipc
        self._write_pid()
        os.environ["NUTSHELL_SESSION_ID"] = self._session_id
        # Reset stale "running" state from a previous crash
        write_session_status(self.system_dir, model_state="idle", model_source="system")

        migrate_legacy_task_sources(self.session_dir)

        # Notify if meta session has a newer version than this session.
        self._emit_version_notice_if_stale()

        # Skip existing context events — only process new user_input events.
        input_offset = ipc.context_size()
        interrupt_offset = ipc.events_size()

        try:
            while True:
                # Check for interrupt control events (soft interrupt).
                interrupted, interrupt_offset = ipc.poll_interrupt(interrupt_offset)
                if interrupted:
                    inputs, input_offset = ipc.poll_inputs(input_offset)
                    if inputs:
                        self._append_event({"type": "interrupted", "discarded": len(inputs)})
                    else:
                        self._append_event({"type": "interrupted", "discarded": 0})
                    await asyncio.sleep(0.5)
                    continue

                # Poll for new user_input events
                inputs, input_offset = ipc.poll_inputs(input_offset)
                for msg in inputs:
                    content = msg.get("content", "")
                    msg_id = msg.get("id")
                    caller_type = msg.get("caller", "human")
                    if self.is_stopped():
                        self.set_status("active")
                        self._append_event({"type": "status", "value": "resumed"})
                    content = self._reshape_history(content)
                    try:
                        await self.chat(content, user_input_id=msg_id, caller_type=caller_type)
                    except Exception as exc:
                        self._append_event({"type": "error", "content": str(exc)})

                # Auto-expire stopped sessions after 5 hours
                if self.is_stopped():
                    st = read_session_status(self.system_dir)
                    stopped_at_str = st.get("stopped_at")
                    if stopped_at_str:
                        try:
                            stopped_at = datetime.fromisoformat(stopped_at_str)
                            now = datetime.now(stopped_at.tzinfo) if stopped_at.tzinfo is not None else datetime.now()
                            elapsed = (now - stopped_at).total_seconds()
                            if elapsed >= 5 * 3600:
                                clear_all_cards(self.tasks_dir)
                                write_session_status(self.system_dir, status="active", stopped_at=None)
                                self._append_event({"type": "status", "value": "auto-expired after 5h stopped"})
                        except Exception:
                            pass

                # Task card scheduling — check for due cards each cycle
                if not self.is_stopped() and not self._agent_lock.locked():
                    due_cards = load_due_cards(self.tasks_dir)
                    for card in due_cards:
                        if self._agent_lock.locked():
                            break
                        try:
                            await self.tick(card)
                        except Exception as exc:
                            self._append_event({"type": "error", "content": str(exc)})

                if stop_event is not None and stop_event.is_set():
                    break
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
    def core_dir(self) -> Path:
        return self.session_dir / "core"

    @property
    def docs_dir(self) -> Path:
        return self.session_dir / "docs"

    @property
    def playground_dir(self) -> Path:
        return self.session_dir / "playground"

    @property
    def system_dir(self) -> Path:
        return self._system_base / self._session_id

    @property
    def memory_path(self) -> Path:
        return self.core_dir / "memory.md"

    @property
    def tasks_dir(self) -> Path:
        return self.core_dir / "tasks"

    @property
    def tasks_path(self) -> Path:
        return self.core_dir / "tasks.md"

    @property
    def _context_path(self) -> Path:
        return self.system_dir / "context.jsonl"

    @property
    def _events_path(self) -> Path:
        return self.system_dir / "events.jsonl"

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
        write_session_status(self.system_dir, **updates)
        return ts

    def _make_tool_call_callback(self):
        """Return (callback, counter) pair for streaming tool call events.

        The callback writes a tool_call event to events.jsonl for each tool
        invoked, giving the UI real-time visibility before results return.
        Composes with the external on_tool_call hook if set.
        The counter reports how many tool calls were streamed (used to mark
        the turn with has_streaming_tools=True so history doesn't duplicate them).
        """
        count: list[int] = [0]
        ext = self.on_tool_call

        def on_tool_call(name: str, input: dict) -> None:
            count[0] += 1
            self._append_event({"type": "tool_call", "name": name, "input": input})
            if ext:
                ext(name, input)

        def get_count() -> int:
            return count[0]

        return on_tool_call, get_count

    def _make_tool_done_callback(self):
        """Return a composed on_tool_done callback.

        Emits a ``tool_done`` event to events.jsonl after each tool execution,
        giving the UI visibility into tool results. Composes with the external
        on_tool_done hook if set.
        """
        ext = self.on_tool_done

        def on_tool_done(name: str, input: dict, result: str) -> None:
            self._append_event({"type": "tool_done", "name": name, "result_len": len(result)})
            if ext:
                ext(name, input, result)

        return on_tool_done

    def _make_loop_start_callback(self):
        """Return a composed on_loop_start callback.

        Emits a ``loop_start`` event to events.jsonl when the agent loop begins.
        Composes with the external on_loop_start hook if set.
        """
        ext = self.on_loop_start

        def on_loop_start(input: str) -> None:
            self._append_event({"type": "loop_start"})
            if ext:
                ext(input)

        return on_loop_start

    def _make_loop_end_callback(self):
        """Return a composed on_loop_end callback.

        Emits a ``loop_end`` event to events.jsonl when the agent loop finishes,
        including iteration count and token usage summary. Composes with the
        external on_loop_end hook if set.
        """
        ext = self.on_loop_end

        def on_loop_end(result: "AgentResult") -> None:
            payload: dict = {"type": "loop_end", "iterations": result.iterations}
            if result.usage and result.usage.total_tokens > 0:
                payload["usage"] = result.usage.as_dict()
            self._append_event(payload)
            if ext:
                ext(result)

        return on_loop_end

    def _emit_version_notice_if_stale(self) -> None:
        """Emit system_notice if the meta session is at a newer version than this session."""
        manifest_path = self.system_dir / "manifest.json"
        if not manifest_path.exists():
            return
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return
        entity_name = manifest.get("entity", "")
        if not entity_name:
            return
        # Skip the meta session itself
        if self._session_id == f"{entity_name}_meta":
            return
        try:
            from nutshell.session_engine.entity_state import get_meta_version
            meta_version = get_meta_version(entity_name)
        except Exception:
            return
        session_version = read_session_status(self.system_dir).get("agent_version")
        if meta_version and session_version and meta_version != session_version:
            self._append_event({
                "type": "system_notice",
                "message": (
                    f"Agent updated to v{meta_version} "
                    f"(this session is on v{session_version}). "
                    "Start a new session to get the latest configuration."
                ),
                "meta_version": meta_version,
                "session_version": session_version,
            })

    def _reshape_history(self, new_content: str) -> str:
        """Clean up orphaned trailing user message before processing new user input.

        If the agent history ends with an unresponded user message (e.g., a
        task prompt interrupted mid-run), we either drop it (if it was a
        task prompt) or merge it with the new message (if it was a real
        user message), to prevent consecutive user messages which the API rejects.
        """
        if not self._agent._history or self._agent._history[-1].role != "user":
            return new_content
        last = self._agent._history[-1]
        last_content = last.content if isinstance(last.content, str) else ""
        self._agent._history.pop()
        if (
            "Task activation:" in last_content
            or last_content.startswith("[Task:")
            or last_content.startswith("Task wakeup:")
            or "Heartbeat activation:" in last_content
            or last_content.startswith("[Heartbeat ")
        ):
            # Orphaned task prompt/marker — drop it, use new message as-is
            return new_content
        # Orphaned real user message — merge with new input
        return f"{last_content}\n\n{new_content}"

    def _make_text_chunk_callback(self):
        """Return a sync callback that writes throttled partial_text events.

        Chunks are buffered and flushed every ~150 characters to limit
        write frequency while still giving the UI near-real-time feedback.
        Composes with the external on_text_chunk hook if set.

        The returned callback has a ``.flush()`` attribute that must be
        called after ``agent.run()`` completes to emit any remaining
        buffered text.  Without this, the last <150-char segment of
        every tool-call iteration would be silently dropped.
        """
        buf: list[str] = []
        buf_len: list[int] = [0]
        ext = self.on_text_chunk
        FLUSH_THRESHOLD = 150

        def on_chunk(chunk: str) -> None:
            buf.append(chunk)
            buf_len[0] += len(chunk)
            if buf_len[0] >= FLUSH_THRESHOLD:
                accumulated = "".join(buf)
                self._append_event({"type": "partial_text", "content": accumulated})
                buf.clear()
                buf_len[0] = 0
            if ext:
                ext(chunk)

        def flush() -> None:
            """Emit any remaining buffered text as a final partial_text event."""
            if buf:
                self._append_event({"type": "partial_text", "content": "".join(buf)})
                buf.clear()
                buf_len[0] = 0

        on_chunk.flush = flush  # type: ignore[attr-defined]
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
