from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from butterfly.core.agent import Agent
from butterfly.core.guardian import Guardian
from butterfly.core.hook import OnLoopEnd, OnLoopStart, OnTextChunk, OnToolCall, OnToolDone
from butterfly.core.tool import Tool
from butterfly.core.types import AgentResult, TokenUsage

_log = logging.getLogger(__name__)
from butterfly.session_engine.pending_inputs import (
    ChatItem,
    TaskItem,
    default_mode_for_source,
)
from butterfly.session_engine.session_config import read_config, ensure_config
from butterfly.session_engine.task_cards import (
    TaskCard, clear_all_cards,
    load_due_cards, save_card,
)
from butterfly.llm_engine.registry import provider_name, resolve_provider
from butterfly.session_engine.session_status import ensure_session_status, read_session_status, write_session_status
from butterfly.tool_engine.background import BackgroundEvent, BackgroundTaskManager
from butterfly.tool_engine.loader import ToolLoader

if TYPE_CHECKING:
    from butterfly.runtime.ipc import FileIPC

SESSIONS_DIR = Path(__file__).parent.parent.parent / "sessions"
_SYSTEM_SESSIONS_DIR = Path(__file__).parent.parent.parent / "_sessions"
SESSION_FINISHED = "SESSION_FINISHED"

# Background-spawn placeholder pattern. Agent.py returns this exact prefix
# from ``_execute_tools`` when a tool was routed to BackgroundTaskManager.spawn:
#
#     Task started. task_id=<tid>. Output will arrive in a later turn …
#
# We anchor on that literal prefix so unrelated tool outputs that happen to
# contain ``task_id="..."`` (e.g. an agent cat'ing a file that mentions an
# earlier tid) don't get mis-tagged as background placeholders — which would
# leave the chat cell yellow forever waiting on a tool_finalize that never
# arrives. Reported in PR #28 review as Bug #3.
_BG_PLACEHOLDER_PREFIX = "Task started. task_id="
_BG_PLACEHOLDER_TID_RE = re.compile(
    r"^Task started\. task_id=([A-Za-z0-9_]+)\."
)


def _parse_background_tid(result: str) -> str | None:
    """Return the tid embedded in a background-spawn placeholder result, else None.

    Only matches the exact placeholder format emitted by
    ``butterfly/core/agent.py::_execute_tools`` — any other string that
    happens to mention ``task_id="..."`` is rejected.
    """
    if not isinstance(result, str) or not result.startswith(_BG_PLACEHOLDER_PREFIX):
        return None
    m = _BG_PLACEHOLDER_TID_RE.match(result)
    return m.group(1) if m else None


class Session:
    """Agent persistent run context (server mode only).

    Disk layout:
        sessions/<id>/                ← agent-visible
          core/
            system.md               ← system prompt (copied from agent at creation)
            task.md                 ← task wakeup prompt
            env.md                  ← session paths + operational guide
            memory.md               ← persistent memory (auto-injected each activation)
            tasks/*.json            ← task cards (JSON with scheduling + status)
            config.yaml             ← runtime config
            tools.md                ← enabled toolhub tools (one name per line)
            skills.md               ← enabled skillhub skills (one name per line)
            tools/                  ← agent-created tools: .json + .sh
            skills/                 ← agent-created skills
          docs/                     ← user-uploaded files
          playground/               ← agent's free workspace

        _sessions/<id>/             ← system-only twin (agent never sees this)
          manifest.json             ← static: agent name, created_at
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

    _INPUT_POLL_INTERVAL = 0.05
    _TASK_POLL_INTERVAL = 0.5

    def __init__(
        self,
        agent: Agent,
        session_id: str | None = None,
        base_dir: Path = SESSIONS_DIR,
        system_base: Path = _SYSTEM_SESSIONS_DIR,
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
        self._agent_lock: asyncio.Lock = asyncio.Lock()
        self._ipc: FileIPC | None = None

        # ── v2.0.12: input dispatcher ────────────────────────────────
        # Inbox of pending ChatItem / TaskItem. Producers (daemon poll loop,
        # background-task drain, chat() callers) enqueue here; a single
        # consumer task drains and runs them serially with cancel-on-interrupt
        # and tail-merge semantics — see docs/butterfly/session_engine/design.md.
        # Lock + event are created lazily on first use so the Session can be
        # constructed outside an event loop (existing test fixtures do this).
        self._inbox: list = []
        self._inbox_lock: asyncio.Lock | None = None
        self._consumer_task: asyncio.Task | None = None
        # Active chat run task and its history-baseline. Used at cancellation
        # time to decide between merge-into-current (uncommitted) vs.
        # save-partial-and-run-new (committed).
        self._run_task: asyncio.Task | None = None
        self._current_chat_item: ChatItem | None = None
        self._run_history_baseline: int = 0
        # Track task names already enqueued so the daemon doesn't requeue
        # the same card every poll cycle while it sits in the inbox.
        self._scheduled_task_names: set[str] = set()

        # External hooks — composed with internal IPC callbacks in chat()/tick()
        self.on_loop_start = on_loop_start
        self.on_loop_end = on_loop_end
        self.on_tool_done = on_tool_done
        self.on_tool_call = on_tool_call
        self.on_text_chunk = on_text_chunk

        # v2.0.19: per-tool start timestamps keyed by ``ToolCall.id`` so the
        # tool_done callback can emit ``duration_ms`` even when tools run
        # concurrently (``asyncio.gather`` in Agent._execute_tools).
        self._tool_started: dict[str, float] = {}
        # v2.0.19: set by ``_make_thinking_callbacks`` while a run is active;
        # consumed by ``_make_llm_call_end_callback`` to stamp the last
        # thinking block of the just-finished LLM call with the
        # provider-reported reasoning_tokens. ``None`` when not in a run.
        self._pending_thinking_attributor = None
        # v2.0.20: monotonic timestamp of the first text chunk of the
        # currently-streaming LLM call. Set by the text-chunk callback on
        # the first chunk; consumed (and reset to None) by on_llm_call_end
        # so it can emit ``agent_output_done`` with the measured output
        # duration. Stays None for calls that don't produce text.
        self._text_output_started_at: float | None = None
        # v2.0.20: per-turn list of output durations (one entry per LLM
        # call that produced text). Populated by on_llm_call_end; drained
        # by the turn writer into ``turn["agent_output_durations"]`` so
        # history replay can pair each text block with its call's duration
        # WITHIN the turn — position-based pairing across the whole
        # events.jsonl was fragile when old turns lacked the instrumentation.
        self._current_turn_agent_durations: list[int] = []
        # v2.0.23: per-turn list of usage snapshots (one entry per LLM call
        # that produced text). Sibling of ``_current_turn_agent_durations``
        # and populated on the same hook — each entry is this call's usage
        # dict. Turn writer drains onto ``turn["agent_output_usages"]`` so
        # the frontend can stamp EACH agent cell with the tokens its own LLM
        # call burnt, instead of the cumulative turn total. Without this a
        # tool-heavy turn (e.g. 5 iterations, 1 final text block) inflated
        # the cached-token pill to 5x the real value because cache_read is
        # the same prefix re-counted per iteration.
        self._current_turn_agent_usages: list[dict] = []
        # v2.0.23 round-6: per-turn list of usage snapshots indexed by
        # iteration — one entry per on_llm_call_end fire (unconditional,
        # NOT filtered by "produced text"). Drains onto
        # ``turn["per_iteration_usages"]`` so the frontend can render a
        # dim token footer inside every cell body (thinking, tool, agent)
        # — each footer shows the usage of the LLM call whose iteration
        # produced the block. Distinct from agent_output_usages which only
        # has entries for text-producing calls; this one is aligned 1:1
        # with turn.messages[assistant].
        self._current_turn_iteration_usages: list[dict] = []

        # Idempotent directory creation — safe for both new and resumed sessions
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.core_dir.mkdir(exist_ok=True)
        (self.core_dir / "tools").mkdir(exist_ok=True)
        (self.core_dir / "skills").mkdir(exist_ok=True)
        self.panel_dir.mkdir(parents=True, exist_ok=True)
        self.docs_dir.mkdir(exist_ok=True)
        self.playground_dir.mkdir(exist_ok=True)
        self.system_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.tool_results_dir.mkdir(parents=True, exist_ok=True)
        if not self.memory_path.exists():
            self.memory_path.write_text("", encoding="utf-8")
        if not self._context_path.exists():
            self._context_path.touch()
        if not self._events_path.exists():
            self._events_path.touch()
        ensure_session_status(self.system_dir)
        ensure_config(self.session_dir)

        # Sub-agent identity from manifest. `mode` (explorer/executor) and
        # `parent_session_id` are written by init_session; mode drives the
        # Guardian wired into write/edit/bash via ToolLoader.
        self._mode: str | None = None
        self._parent_session_id: str | None = None
        self._guardian: Guardian | None = None
        manifest_path = self.system_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self._mode = manifest.get("mode")
                self._parent_session_id = manifest.get("parent_session_id")
            except (json.JSONDecodeError, OSError):
                pass
        if self._mode == "explorer":
            self._guardian = Guardian(self.playground_dir)

        # Non-blocking tool infrastructure — one BackgroundTaskManager per session.
        # The manager is wired into the Agent so backgroundable tool calls with
        # run_in_background=true get routed here instead of executed inline.
        def _venv_env_provider() -> dict[str, str] | None:
            try:
                from butterfly.tool_engine.executor.terminal.bash_terminal import _venv_env
                return _venv_env()
            except Exception:
                return None

        self._bg_manager = BackgroundTaskManager(
            panel_dir=self.panel_dir,
            tool_results_dir=self.tool_results_dir,
            venv_env_provider=_venv_env_provider,
            guardian=self._guardian,
        )
        self._agent.background_spawn = self._bg_manager.spawn

        # Sub-agent runner: lets ``sub_agent`` calls with run_in_background=true
        # flow through the same panel + events plumbing as bash. Sync calls
        # use SubAgentTool directly via ToolLoader.
        from butterfly.tool_engine.sub_agent import SubAgentRunner
        self._bg_manager.register_runner("sub_agent", SubAgentRunner(
            parent_session_id=self._session_id,
            sessions_base=self._base_dir,
            system_sessions_base=self._system_base,
            agent_base=self._base_dir.parent / "agenthub",
        ))

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
        from butterfly.skill_engine.loader import SkillLoader

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
        env_md = self._read_core_text("env.md")
        # Sub-agent mode prompt — folded into the static (cacheable) system
        # prefix so explorer/executor identity is established before env_context.
        # Empty string when this isn't a sub-agent session.
        mode_md = self._read_core_text("mode.md")

        if mode_md:
            self._agent.system_prompt = f"{system_md}\n\n---\n\n{mode_md}" if system_md else mode_md
        else:
            self._agent.system_prompt = system_md
        self._agent.env_context = (
            env_md.replace("{session_id}", self._session_id) if env_md else ""
        )
        self._agent.task_prompt = self._read_core_text("task.md")
        self._agent.memory = self.memory_path.read_text(encoding="utf-8").strip()

        # v2.0.5 memory (β): sub-memory under core/memory/*.md is NO LONGER
        # injected into the system prompt. The agent discovers sub-memories via
        # one-line index entries in main memory.md and fetches them on demand
        # via memory_recall. See docs/butterfly/session_engine/design.md.

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
        # default_workdir: tools run from the session directory so agents use
        # short relative paths (core/tasks/) instead of full session paths.
        try:
            loader = ToolLoader(
                default_workdir=str(self.session_dir),
                skills=skills,
                tasks_dir=self.tasks_dir,
                memory_dir=self.core_dir / "memory",
                main_memory_path=self.memory_path,
                panel_dir=self.panel_dir,
                tool_results_dir=self.tool_results_dir,
                guardian=self._guardian,
                parent_session_id=self._session_id,
                sessions_base=self._base_dir,
                system_sessions_base=self._system_base,
                agent_base=self._base_dir.parent / "agenthub",
            )
            # Load tools from tools.md (toolhub), fallback to legacy tool.md
            tools_md_path = self.core_dir / "tools.md"
            legacy_tool_md = self.core_dir / "tool.md"
            selected_tools_md = tools_md_path if tools_md_path.exists() else legacy_tool_md
            if selected_tools_md.exists():
                tools = loader.load_from_tool_md(selected_tools_md)
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
            "text":              {"type", "text"},
            "tool_use":          {"type", "id", "name", "input"},
            "tool_result":       {"type", "tool_use_id", "content", "is_error"},
            "image":             {"type", "source"},
            # Moonshot/Kimi reasoning echoed back on tool-call turns when
            # thinking is enabled. Preserved through reload so the next LLM
            # call still carries the field — skipping it 400s on Kimi.
            "reasoning_content": {"type", "text"},
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
        from butterfly.core.types import Message
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
                    from butterfly.skill_engine.loader import _parse_frontmatter
                    text = Path(skill.location).read_text(encoding="utf-8")
                    _, body = _parse_frontmatter(text)
                else:
                    body = skill.body
                header = f"[Skill: {skill.name}]\n\n{body.strip()}"
                return f"{header}\n\n---\n\n{args}" if args else header
        return message

    # ── Public chat / tick (queue-routed) ──────────────────────────

    async def chat(
        self,
        message: str,
        *,
        user_input_id: str | None = None,
        caller_type: str = "human",
        mode: str = "interrupt",
        source: str = "user",
    ) -> AgentResult:
        """Submit a user message and await the resulting AgentResult.

        v2.0.12: chat is now dispatcher-routed. The call enqueues a
        ``ChatItem`` and waits on its future; the consumer loop handles
        merging and cancellation per the inbox semantics. For a single
        caller with no concurrent traffic the observable behaviour is
        unchanged from prior versions — the consumer pulls the lone item
        and runs it directly.

        Args:
            mode: ``interrupt`` (default) cancels the in-flight run if any
                and runs this content next; if the cancelled run had not
                yet committed an assistant turn, the cancelled content is
                merged into this content (avoids consecutive user msgs on
                the LLM API). ``wait`` queues behind any in-flight or
                earlier-queued items and merges with any adjacent
                wait-mode chat item.
            source: ``user`` / ``panel`` (background tool) / ``task``.
                Used for telemetry and to default ``mode`` if not set.
            caller_type: ``human`` / ``agent`` / ``system`` — forwarded to
                ``Agent.run(caller_type=)`` for prompt adaptation.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        item = ChatItem(
            content=message,
            mode=mode,
            source=source,
            caller_type=caller_type,
            user_input_ids=[user_input_id] if user_input_id else [],
            futures=[future],
        )
        await self._enqueue(item)
        return await future

    async def tick(self, card: TaskCard | None = None) -> AgentResult | None:
        """Execute a single task card (or the next due card).

        If ``card`` is omitted, picks the first due card from ``core/tasks/``;
        returns ``None`` if nothing is due. v2.0.12: routed through the
        dispatcher as a ``TaskItem`` (always wait mode), so a chat in flight
        completes before the wakeup fires and a follow-up interrupt-chat
        cleanly cancels the wakeup and resets the card to ``pending``.
        """
        if card is None:
            due = load_due_cards(self.tasks_dir)
            if due:
                card = due[0]
            else:
                return None
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        item = TaskItem(card=card, futures=[future])
        await self._enqueue(item)
        return await future

    # ── Dispatcher (consumer + enqueue + cancel/merge wiring) ──────

    def _ensure_inbox_primitives(self) -> None:
        """Create the inbox lock lazily inside the running event loop."""
        if self._inbox_lock is None:
            self._inbox_lock = asyncio.Lock()

    async def _enqueue(self, item) -> None:
        """Append item to the inbox and (re)start the consumer if idle.

        Wait-mode chat items merge into the trailing wait-mode chat item
        if one exists, so a burst of follow-up sends collapses into a
        single user turn. Interrupt-mode chat items always append; if a
        run is in flight, they trigger cancellation here.
        """
        self._ensure_inbox_primitives()
        async with self._inbox_lock:
            merged = False
            if isinstance(item, ChatItem) and item.mode == "wait":
                if (
                    self._inbox
                    and isinstance(self._inbox[-1], ChatItem)
                    and self._inbox[-1].mode == "wait"
                ):
                    self._inbox[-1].merge_after(item)
                    merged = True
            if not merged:
                if isinstance(item, TaskItem):
                    self._scheduled_task_names.add(item.card.name)
                self._inbox.append(item)
            # Cancel current run for interrupt-mode chat items. The consumer
            # observes the CancelledError and either merges the cancelled
            # input back into ``item`` (uncommitted) or saves a partial
            # turn and runs ``item`` fresh (committed).
            if (
                isinstance(item, ChatItem)
                and item.mode == "interrupt"
                and self._run_task is not None
                and not self._run_task.done()
            ):
                self._run_task.cancel()
            # Kick the consumer if it has gone idle.
            if self._consumer_task is None or self._consumer_task.done():
                self._consumer_task = asyncio.create_task(self._consumer_loop())

    async def _consumer_loop(self) -> None:
        """Drain the inbox sequentially, merging wait-tail items per pop."""
        try:
            while True:
                item = None
                async with self._inbox_lock:
                    if not self._inbox:
                        # No more work — exit so the event loop can shut down
                        # cleanly. ``_enqueue`` will start a new consumer when
                        # the next item arrives.
                        return
                    item = self._inbox.pop(0)
                    # Greedy wait-tail merge: pull all consecutive wait-mode
                    # ChatItems that landed behind this one in the same poll
                    # cycle so they share a single LLM turn.
                    while (
                        isinstance(item, ChatItem)
                        and self._inbox
                        and isinstance(self._inbox[0], ChatItem)
                        and self._inbox[0].mode == "wait"
                    ):
                        nxt = self._inbox.pop(0)
                        item.merge_after(nxt)
                await self._dispatch_one(item)
        except asyncio.CancelledError:
            async with self._inbox_lock:
                for it in self._inbox:
                    it.reject(asyncio.CancelledError())
                self._inbox.clear()
                self._scheduled_task_names.clear()
            raise

    async def _dispatch_one(self, item) -> None:
        """Run a single item; on CancelledError, route per uncommitted/committed."""
        if isinstance(item, TaskItem):
            try:
                result = await self._do_tick(item.card)
                item.resolve(result)
            except asyncio.CancelledError:
                # Task was interrupted. Reset the card so it fires again on
                # next due check; the cancelled wakeup content is discarded
                # (task prompts don't textually merge with chat content).
                try:
                    item.card.mark_pending()
                    save_card(self.tasks_dir, item.card)
                except Exception:
                    pass
                item.reject(asyncio.CancelledError())
            except BaseException as exc:
                item.reject(exc)
            finally:
                self._scheduled_task_names.discard(item.card.name)
            return

        # ChatItem dispatch
        self._current_chat_item = item
        self._run_history_baseline = len(self._agent._history)
        self._run_task = asyncio.create_task(self._do_chat(item))
        try:
            result = await self._run_task
            item.resolve(result)
        except asyncio.CancelledError:
            committed = len(self._agent._history) > self._run_history_baseline
            if not committed:
                # Uncommitted → fold our content into the next interrupt-mode
                # chat item so they go to the LLM as one user message.
                merged = False
                async with self._inbox_lock:
                    for nxt in self._inbox:
                        if isinstance(nxt, ChatItem) and nxt.mode == "interrupt":
                            nxt.merge_before(item)
                            merged = True
                            break
                if not merged:
                    # No follow-up to absorb us — caller's chat() rejects.
                    item.reject(asyncio.CancelledError())
            else:
                # Committed → partial turn already saved by _do_chat's
                # cancellation handler; caller's future rejects so they
                # know the run was preempted.
                item.reject(asyncio.CancelledError())
        except BaseException as exc:
            item.reject(exc)
        finally:
            self._current_chat_item = None
            self._run_task = None

    # ── Core run bodies ────────────────────────────────────────────

    async def _do_chat(self, item: ChatItem) -> AgentResult:
        """Execute a chat item end-to-end (capabilities → agent.run → write turn)."""
        message = self._expand_slash_command(item.content)
        # Last-line defence against an orphan trailing user/task marker that
        # somehow survived (e.g. crash recovery between a turn and its next
        # input). The dispatcher's uncommitted-merge handles new arrivals,
        # but reload-from-disk leaves us with only the on-disk history.
        message = self._reshape_history(message)
        old_len = len(self._agent._history)
        self._set_model_status("running", "user")
        self._current_turn_agent_durations = []
        self._current_turn_agent_usages = []
        self._current_turn_iteration_usages = []
        tool_call_cb, get_tool_call_count = self._make_tool_call_callback()
        on_chunk = self._make_text_chunk_callback()
        on_thinking_start, on_thinking_end, had_thinking, get_thinking_blocks = self._make_thinking_callbacks()
        result: AgentResult | None = None
        try:
            async with self._agent_lock:
                self._load_session_capabilities()
                result = await self._agent.run(
                    message,
                    on_text_chunk=on_chunk,
                    on_thinking_start=on_thinking_start,
                    on_thinking_end=on_thinking_end,
                    on_tool_call=tool_call_cb,
                    on_tool_done=self._make_tool_done_callback(),
                    on_loop_start=self._make_loop_start_callback(),
                    on_loop_end=self._make_loop_end_callback(),
                    on_llm_call_end=self._make_llm_call_end_callback(),
                    caller_type=item.caller_type,
                )
        except asyncio.CancelledError:
            on_chunk.flush()
            self._set_model_status("idle", "user")
            self._save_partial_chat_turn(
                item, old_len, get_tool_call_count(), had_thinking(),
                thinking_blocks=get_thinking_blocks(),
            )
            raise
        except BaseException:
            on_chunk.flush()
            self._set_model_status("idle", "user")
            raise
        finally:
            on_chunk.flush()
            # Drop the per-run thinking attributor so a subsequent run (which
            # reinstalls its own) never picks up a stale reference from the
            # prior run's closure.
            self._pending_thinking_attributor = None
            # v2.0.20: clear any stranded text-output start timestamp (e.g.
            # the run was cancelled before on_llm_call_end could drain it).
            # The per-turn agent durations list is NOT cleared here — the
            # chat/tick writers read it immediately after this finally to
            # stamp ``turn["agent_output_durations"]``. It's reset at the
            # START of the next _do_chat / _do_tick.
            self._text_output_started_at = None

        self._save_chat_turn(
            item, old_len, result, get_tool_call_count(), had_thinking(),
            thinking_blocks=get_thinking_blocks(),
        )
        self._set_model_status("idle", "user")
        return result

    async def _do_tick(self, card: TaskCard) -> AgentResult | None:
        """Execute a task card (was tick() body pre-v2.0.12)."""
        triggered_by = f"task:{card.name}"
        task_info = card.description or card.name

        # Snapshot history so we can roll back on SESSION_FINISHED
        history_snapshot = list(self._agent._history)
        old_len = len(self._agent._history)

        task_prompt = self._agent.task_prompt
        if task_prompt and "{task}" in task_prompt:
            prompt = task_prompt.format(task=task_info)
        else:
            prompt = f"Task wakeup: {card.name}\n\n{task_info}"
            if task_prompt:
                prompt += f"\n\n{task_prompt}"

        trigger_ts = datetime.now().isoformat()
        # v2.0.23: write the wakeup marker to context.jsonl (not events.jsonl).
        # events.jsonl isn't replayed on history fetch, so the previous
        # placement meant the "Wakeup" card vanished whenever the user
        # reloaded a session. context.jsonl survives reload; the dispatcher's
        # ``poll_inputs`` filters by ``type == "user_input"`` so this marker
        # does NOT re-enqueue the task into the run queue (the TaskItem
        # that owns this tick is already in flight). ``_context_event_to_display``
        # picks up the marker and emits a ``user`` display event with
        # ``caller=task`` so the frontend renders the sky-blue Wakeup card
        # uniformly on live AND history paths.
        self._append_context({
            "type": "task_wakeup",
            "card": card.name,
            "prompt": prompt,
            "ts": trigger_ts,
        })
        card.mark_working()
        save_card(self.tasks_dir, card)
        self._set_model_status("running", triggered_by)
        self._current_turn_agent_durations = []
        self._current_turn_agent_usages = []
        self._current_turn_iteration_usages = []
        tool_call_cb, get_tool_call_count = self._make_tool_call_callback()
        on_chunk = self._make_text_chunk_callback()
        on_thinking_start, on_thinking_end, had_thinking, get_thinking_blocks = self._make_thinking_callbacks()
        try:
            async with self._agent_lock:
                self._load_session_capabilities()
                result = await self._agent.run(
                    prompt,
                    on_text_chunk=on_chunk,
                    on_thinking_start=on_thinking_start,
                    on_thinking_end=on_thinking_end,
                    on_tool_call=tool_call_cb,
                    on_tool_done=self._make_tool_done_callback(),
                    on_loop_start=self._make_loop_start_callback(),
                    on_loop_end=self._make_loop_end_callback(),
                    on_llm_call_end=self._make_llm_call_end_callback(),
                )
        except asyncio.CancelledError:
            # On cancel we roll back the per-iteration commits the agent
            # may have written so the verbose task prompt does not pollute
            # history. The card is marked pending by ``_dispatch_one``.
            self._agent._history = history_snapshot
            self._set_model_status("idle", triggered_by)
            on_chunk.flush()
            raise
        except BaseException:
            card.mark_pending()
            save_card(self.tasks_dir, card)
            self._set_model_status("idle", triggered_by)
            on_chunk.flush()
            raise
        finally:
            on_chunk.flush()
            # See note in _do_chat — drop per-run attributor reference.
            self._pending_thinking_attributor = None
            # Mirror _do_chat's reset: a tick cancelled between on_chunk's
            # first-chunk stamp and on_llm_call_end would otherwise leak the
            # monotonic timestamp into the next run, making its first
            # agent_output_done duration include the dead time between runs.
            self._text_output_started_at = None

        if SESSION_FINISHED in result.content:
            clear_all_cards(self.tasks_dir)
            self._agent._history = history_snapshot
            self._append_event({"type": "task_finished", "card": card.name, "ts": trigger_ts})
        else:
            card.mark_finished()
            save_card(self.tasks_dir, card)

            new_msgs = self._agent._history[old_len:]
            if new_msgs and new_msgs[0].role == "user":
                from butterfly.core.types import Message as _Msg
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
                if had_thinking():
                    turn["has_streaming_thinking"] = True
                thinking_blocks = get_thinking_blocks()
                if thinking_blocks:
                    turn["thinking_blocks"] = thinking_blocks
                if self._current_turn_agent_durations:
                    turn["agent_output_durations"] = list(self._current_turn_agent_durations)
                if self._current_turn_agent_usages:
                    turn["agent_output_usages"] = list(self._current_turn_agent_usages)
                if self._current_turn_iteration_usages:
                    turn["per_iteration_usages"] = list(self._current_turn_iteration_usages)
                if result.usage and result.usage.total_tokens > 0:
                    turn["usage"] = result.usage.as_dict()
                self._append_context(turn)

        self._set_model_status("idle", triggered_by)
        return result

    # ── Turn writers (success + interrupted-with-commit) ───────────

    def _save_chat_turn(
        self,
        item: ChatItem,
        old_len: int,
        result: AgentResult,
        tool_call_count: int,
        had_thinking: bool,
        *,
        thinking_blocks: list[dict] | None = None,
    ) -> None:
        turn: dict = {
            "type": "turn",
            "triggered_by": "user",
            "messages": self._serialize_turn_messages(result.messages[old_len:]),
        }
        if item.latest_user_input_id:
            turn["user_input_id"] = item.latest_user_input_id
        if len(item.user_input_ids) > 1:
            turn["merged_user_input_ids"] = list(item.user_input_ids)
        if tool_call_count > 0:
            turn["has_streaming_tools"] = True
        if had_thinking:
            turn["has_streaming_thinking"] = True
        if thinking_blocks:
            turn["thinking_blocks"] = thinking_blocks
        if self._current_turn_agent_durations:
            turn["agent_output_durations"] = list(self._current_turn_agent_durations)
        if self._current_turn_agent_usages:
            turn["agent_output_usages"] = list(self._current_turn_agent_usages)
        if self._current_turn_iteration_usages:
            turn["per_iteration_usages"] = list(self._current_turn_iteration_usages)
        if result.usage and result.usage.total_tokens > 0:
            turn["usage"] = result.usage.as_dict()
        self._append_context(turn)

    def _save_partial_chat_turn(
        self,
        item: ChatItem,
        old_len: int,
        tool_call_count: int,
        had_thinking: bool,
        *,
        thinking_blocks: list[dict] | None = None,
    ) -> None:
        """Persist the committed-but-cancelled prefix of a chat turn.

        Called from ``_do_chat`` when the agent loop raises CancelledError
        after at least one iteration was committed to history. Without this,
        the in-progress assistant text + tool calls would be invisible to
        future SSE clients (only ``agent._history`` would remember, and
        only until the next reload).
        """
        partial = self._agent._history[old_len:]
        if not partial:
            return
        turn: dict = {
            "type": "turn",
            "triggered_by": "user",
            "interrupted": True,
            "messages": self._serialize_turn_messages(partial),
        }
        if item.latest_user_input_id:
            turn["user_input_id"] = item.latest_user_input_id
        if len(item.user_input_ids) > 1:
            turn["merged_user_input_ids"] = list(item.user_input_ids)
        if tool_call_count > 0:
            turn["has_streaming_tools"] = True
        if had_thinking:
            turn["has_streaming_thinking"] = True
        if thinking_blocks:
            turn["thinking_blocks"] = thinking_blocks
        if self._current_turn_agent_durations:
            turn["agent_output_durations"] = list(self._current_turn_agent_durations)
        if self._current_turn_agent_usages:
            turn["agent_output_usages"] = list(self._current_turn_agent_usages)
        if self._current_turn_iteration_usages:
            turn["per_iteration_usages"] = list(self._current_turn_iteration_usages)
        self._append_context(turn)

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
            from butterfly.runtime.git_coordinator import GitCoordinator
            coordinator = GitCoordinator(system_base=self._system_base)
            coordinator.release(self._session_id)
        except Exception:
            pass  # best-effort cleanup

    # ── Server loop ────────────────────────────────────────────────

    async def run_daemon_loop(self, ipc: "FileIPC", stop_event: asyncio.Event | None = None) -> None:
        """Run as a server-managed session.

        Polls ``context.jsonl`` for user_input / interrupt events on a tight
        cadence (default 50 ms) so a human follow-up can cancel the in-flight
        run before a fast provider finishes. Slower housekeeping work (stopped
        auto-expiry + due task scheduling) stays on a coarser cadence. Each
        signal is enqueued into the dispatcher inbox; the consumer loop runs
        them serially with the merge / interrupt semantics in
        ``pending_inputs.py``.

        v2.0.12: the daemon loop no longer awaits ``self.chat()`` /
        ``self.tick()`` directly — the consumer task does. This is what
        lets a fresh ``mode=interrupt`` arrival cancel an in-flight run
        instead of waiting in a serial poll-then-await line.
        """
        self._ipc = ipc
        self._write_pid()
        os.environ["BUTTERFLY_SESSION_ID"] = self._session_id
        write_session_status(self.system_dir, model_state="idle", model_source="system")

        self._emit_version_notice_if_stale()
        self._bg_manager.sweep_restart()

        # v2.0.13 fix (PR #28 review Bug #1): start input_offset at the byte
        # position immediately after the last committed ``turn`` in
        # ``context.jsonl``, not at end-of-file.
        #
        # Motivation: ``init_session(initial_message=...)`` writes a
        # ``user_input`` row BEFORE the watcher starts our daemon. If we
        # initialised input_offset to ``context_size()``, that row would
        # already be past the offset and ``poll_inputs`` would never surface
        # it — the child would sit idle while the parent's ``_wait_for_reply``
        # times out. Rewinding to "after the last turn" keeps resume
        # behaviour correct (turns already processed are not re-enqueued)
        # while guaranteeing fresh sessions pick up their seed inputs.
        input_offset = self._initial_input_offset()
        interrupt_offset = ipc.events_size()
        loop = asyncio.get_running_loop()
        next_housekeeping_at = loop.time()

        try:
            while True:
                self._drain_background_events()

                # Explicit interrupt control event (bare interrupt — distinct
                # from chat-with-mode=interrupt). Cancels the in-flight run
                # AND drops everything queued.
                interrupted, interrupt_offset = ipc.poll_interrupt(interrupt_offset)
                if interrupted:
                    inputs, input_offset = ipc.poll_inputs(input_offset)
                    discarded = len(inputs)
                    await self._handle_explicit_interrupt(discarded)
                else:
                    inputs, input_offset = ipc.poll_inputs(input_offset)
                    for msg in inputs:
                        content = msg.get("content", "")
                        msg_id = msg.get("id")
                        caller_type = msg.get("caller", "human")
                        source = msg.get("source") or ("user" if caller_type == "human" else "user")
                        mode = msg.get("mode") or default_mode_for_source(source)
                        if mode not in ("interrupt", "wait"):
                            mode = default_mode_for_source(source)
                        if self.is_stopped():
                            self.set_status("active")
                            self._append_event({"type": "status", "value": "resumed"})
                        item = ChatItem(
                            content=content,
                            mode=mode,
                            source=source,
                            caller_type=caller_type,
                            user_input_ids=[msg_id] if msg_id else [],
                        )
                        await self._enqueue(item)

                now = loop.time()
                if now >= next_housekeeping_at:
                    if self.is_stopped():
                        st = read_session_status(self.system_dir)
                        stopped_at_str = st.get("stopped_at")
                        if stopped_at_str:
                            try:
                                stopped_at = datetime.fromisoformat(stopped_at_str)
                                current = datetime.now(stopped_at.tzinfo) if stopped_at.tzinfo is not None else datetime.now()
                                elapsed = (current - stopped_at).total_seconds()
                                if elapsed >= 5 * 3600:
                                    clear_all_cards(self.tasks_dir)
                                    write_session_status(self.system_dir, status="active", stopped_at=None)
                                    self._append_event({"type": "status", "value": "auto-expired after 5h stopped"})
                            except Exception:
                                pass

                    # Task card scheduling — enqueue at most once per card while
                    # it sits in the inbox or is currently running.
                    if not self.is_stopped():
                        due_cards = load_due_cards(self.tasks_dir)
                        for card in due_cards:
                            if card.name in self._scheduled_task_names:
                                continue
                            await self._enqueue(TaskItem(card=card))

                    next_housekeeping_at = now + self._TASK_POLL_INTERVAL

                if stop_event is not None and stop_event.is_set():
                    break
                await asyncio.sleep(self._INPUT_POLL_INTERVAL)

        except asyncio.CancelledError:
            self._set_model_status("idle", "system")
            self._append_event({"type": "status", "value": "cancelled"})
            await self._shutdown_consumer()
            await self._shutdown_background_manager()
            self._clear_pid()
            raise

        self._set_model_status("idle", "system")
        self._append_event({"type": "status", "value": "stopped"})
        await self._shutdown_consumer()
        await self._shutdown_background_manager()
        self._clear_pid()

    async def _handle_explicit_interrupt(self, discarded_inbound: int) -> None:
        """Bare-interrupt handler: cancel the in-flight run and drop the inbox.

        Bound to the ``send_interrupt()`` control event — different from a
        chat with ``mode=interrupt``. A bare interrupt clears everything
        and runs nothing in its place.
        """
        # Seed the lock now so subsequent ``_enqueue`` calls share the same
        # instance. ``self._inbox_lock or asyncio.Lock()`` would have created
        # a throwaway lock here that doesn't synchronize with the producer's
        # in-progress _enqueue on a racing daemon tick (cubic review P2).
        self._ensure_inbox_primitives()
        cancelled_run = False
        dropped = 0
        async with self._inbox_lock:  # type: ignore[arg-type]
            if self._run_task is not None and not self._run_task.done():
                self._run_task.cancel()
                cancelled_run = True
            if self._inbox:
                for it in self._inbox:
                    it.reject(asyncio.CancelledError())
                dropped = len(self._inbox)
                self._inbox.clear()
                self._scheduled_task_names.clear()
        self._append_event({
            "type": "interrupted",
            "discarded": discarded_inbound + dropped,
            "cancelled_run": cancelled_run,
        })

    async def _shutdown_consumer(self) -> None:
        """Cancel the dispatcher consumer + reject any orphan futures."""
        consumer = self._consumer_task
        if consumer is not None and not consumer.done():
            consumer.cancel()
            try:
                await consumer
            except (asyncio.CancelledError, Exception):
                pass
        if self._inbox_lock is not None:
            async with self._inbox_lock:
                for it in self._inbox:
                    it.reject(asyncio.CancelledError())
                self._inbox.clear()
                self._scheduled_task_names.clear()

    async def _shutdown_background_manager(self) -> None:
        """Best-effort cancel of in-flight bg asyncio tasks on daemon exit.

        Running subprocesses themselves keep going (Python can't sync-join
        detached processes here); the next daemon startup marks any still-
        `running` panel entries as `killed_by_restart`.
        """
        try:
            await self._bg_manager.shutdown()
        except Exception as exc:
            self._append_event({"type": "error", "content": f"bg_manager shutdown: {exc}"})

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
    def panel_dir(self) -> Path:
        return self.core_dir / "panel"

    @property
    def tool_results_dir(self) -> Path:
        return self.system_dir / "tool_results"

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

    def _drain_background_events(self) -> None:
        """Non-blocking drain of the BackgroundTaskManager event queue.

        Each event is appended ONCE to `context.jsonl` as a user-role message
        so the agent picks it up on its next wake — append-once avoids the
        O(turns) reminder-bloat bug Claude Code hit (issue #13249). A mirror
        `panel_update` event is also emitted on `events.jsonl` for the UI.
        """
        queue = self._bg_manager.events
        while True:
            try:
                evt: BackgroundEvent = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            entry = evt.entry
            is_sub_agent = entry.tool_name == "sub_agent"
            # Build the human-readable notification text. Kept concise on
            # purpose — bulk output is fetchable via tool_output(task_id=...).
            if evt.kind == "completed":
                duration = ""
                if entry.finished_at and entry.started_at:
                    duration = f" in {entry.finished_at - entry.started_at:.1f}s"
                if is_sub_agent:
                    # Sub-agent contract: parent only ever sees the child's
                    # final reply. The runner stuffs that into entry.meta.result.
                    # v2.0.23: header reformatted to lead with mode +
                    # display_name instead of the opaque tid — both fields
                    # are populated on entry.meta at spawn time by
                    # SubAgentRunner.run. The previous `[sub_agent · child=…
                    # · mode=…]` wrapper inside sub_agent.py::_format_result
                    # was also dropped in this release, so the reply body is
                    # now clean LLM text.
                    sub_result = (entry.meta or {}).get("result") or "(empty reply)"
                    _sub_meta = entry.meta or {}
                    _sub_display = _sub_meta.get("display_name") or entry.tid
                    _sub_mode = _sub_meta.get("mode") or ""
                    _sub_mode_str = f" {_sub_mode}" if _sub_mode else ""
                    msg = (
                        f"sub_agent{_sub_mode_str} {_sub_display} completed{duration}.\n\n"
                        f"{sub_result}"
                    )
                else:
                    msg = (
                        f"Background task {entry.tid} ({entry.tool_name}) completed "
                        f"with exit {entry.exit_code}{duration}. {entry.output_bytes}B output.\n"
                        f'Fetch full output: tool_output(task_id="{entry.tid}").'
                    )
            elif evt.kind == "stalled":
                msg = (
                    f"Background task {entry.tid} ({entry.tool_name}) has produced "
                    "no output for 5 minutes — possibly stuck on interactive input or "
                    "deadlocked. Consider checking its tail with "
                    f'tool_output(task_id="{entry.tid}") and killing it via '
                    "`butterfly panel --tid <tid> --kill` if needed."
                )
            elif evt.kind == "progress":
                msg = (
                    f"Background task {entry.tid} ({entry.tool_name}) progress "
                    f"(new output, {len(evt.delta_text)}B):\n{evt.delta_text.rstrip()}"
                )
            elif evt.kind == "killed_by_restart":
                msg = (
                    f"Background task {entry.tid} ({entry.tool_name}) was running "
                    "when the server restarted and has been terminated. Its partial "
                    f'output is at tool_output(task_id="{entry.tid}").'
                )
            else:
                msg = f"Background task {entry.tid} event: {evt.kind}"

            # For sub_agent, the parent only cares about the FINAL reply —
            # progress lines would just spam the context window. Skip the
            # context append for progress; let the panel + tool_progress
            # events carry that information to the UI only.
            skip_context = is_sub_agent and evt.kind in ("progress", "stalled")
            if not skip_context:
                event = {
                    "type": "user_input",
                    "content": msg,
                    "id": str(uuid.uuid4()),
                    "caller": "system",
                    "source": "panel",
                    # Per spec: background-tool notifications default to interrupt
                    # so the agent surfaces a completed/stalled job promptly even
                    # if it was mid-loop on something else. The dispatcher's
                    # uncommitted-merge rule folds the cancelled in-flight input
                    # back together when no LLM response was committed yet, so
                    # this never produces consecutive user messages on the API.
                    "mode": "interrupt",
                    "tid": entry.tid,
                    "kind": evt.kind,
                    # v2.0.23: carry the originating tool name so the web UI's
                    # background-tool-output card can render the dim sub-label
                    # ("tool output — bash") without reconstructing it from
                    # the free-form message text.
                    "tool_name": entry.tool_name,
                }
                # v2.0.23: sub_agent completions get a dedicated "Sub-agent"
                # notification cell (orange, metallic). The frontend branches
                # on ``tool_name == "sub_agent"`` and keys the dim sub-label
                # on ``display_name``; the ``sub_agent_mode`` field is
                # reserved for a future sub-label extension. We don't overload
                # the pre-existing ``mode`` key — that one carries dispatcher
                # semantics (interrupt/wait) and mustn't be repurposed.
                if is_sub_agent:
                    _meta = entry.meta or {}
                    _dn = _meta.get("display_name")
                    _md = _meta.get("mode")
                    if _dn:
                        event["display_name"] = _dn
                    if _md:
                        event["sub_agent_mode"] = _md
                self._append_context(event)
            self._append_event({
                "type": "panel_update",
                "tid": entry.tid,
                "kind": evt.kind,
                "status": entry.status,
            })

            # Bridge events for the chat-side tool cell: progress keeps the
            # cell yellow with a refreshed summary; terminal kinds flip it
            # to done. Frontend keys both by tid (set on the immediate
            # placeholder tool_done) so this works for any backgroundable tool.
            if evt.kind == "progress":
                summary = ""
                if is_sub_agent:
                    summary = (entry.meta or {}).get("last_child_state", "") or ""
                else:
                    summary = (evt.delta_text or "").strip().splitlines()[-1] if evt.delta_text else ""
                self._append_event({
                    "type": "tool_progress",
                    "tid": entry.tid,
                    "name": entry.tool_name,
                    "summary": summary,
                })
            elif evt.kind in ("completed", "stalled", "killed", "killed_by_restart"):
                # tool_finalize tells the chat cell to leave the working
                # state and render terminal styling.
                duration_ms = 0
                if entry.finished_at and entry.started_at:
                    duration_ms = int((entry.finished_at - entry.started_at) * 1000)
                # v2.0.24: carry the real final output so the live tool cell
                # flips from the "Task started. task_id=…" placeholder to
                # actual bash stdout / sub-agent reply. Without this the
                # cell stayed on the placeholder until a history reload
                # paired the tool_use with its final tool_result.
                #   * sub_agent → entry.meta["result_text"] (child's reply)
                #   * bash bg  → tail of entry.output_file
                # Cap the payload so a huge bash dump doesn't bloat SSE; the
                # full output is still fetchable via tool_output(task_id=...).
                final_result: str | None = None
                _RESULT_MAX = 8000
                if is_sub_agent:
                    rt = (entry.meta or {}).get("result_text")
                    if isinstance(rt, str) and rt:
                        final_result = rt
                elif entry.output_file:
                    try:
                        opath = Path(entry.output_file)
                        if opath.exists():
                            with opath.open("r", encoding="utf-8", errors="replace") as _f:
                                final_result = _f.read()
                    except OSError:
                        final_result = None
                payload: dict = {
                    "type": "tool_finalize",
                    "tid": entry.tid,
                    "name": entry.tool_name,
                    "kind": evt.kind,
                    "duration_ms": duration_ms,
                    "exit_code": entry.exit_code,
                }
                if final_result is not None:
                    truncated = len(final_result) > _RESULT_MAX
                    payload["result"] = final_result[:_RESULT_MAX]
                    if truncated:
                        payload["result_truncated"] = True
                self._append_event(payload)

            # HUD sub-agent counter: any sub_agent state change re-broadcasts
            # the running tally (panel is the source of truth).
            if is_sub_agent:
                self._emit_sub_agent_count()

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

        v2.0.19: the paired ``_make_tool_done_callback`` reads ``_started``
        (keyed by tool_use_id) to compute per-call duration. Tools run
        concurrently via ``asyncio.gather`` in the agent loop, so keying on
        name-alone would misattribute durations when the same tool appears
        twice in one iteration.
        """
        count: list[int] = [0]
        ext = self.on_tool_call
        import time as _time

        def on_tool_call(name: str, input: dict, tool_use_id: str) -> None:
            count[0] += 1
            self._tool_started[tool_use_id] = _time.monotonic()
            # v2.0.23 round-7: carry ``tool_use_id`` so the frontend can key
            # the live tool cell DOM by id (not just by name). Required for
            # the ``iteration_usage`` live-footer signal to target the right
            # cell when two calls of the same tool run concurrently via
            # ``asyncio.gather`` in one iteration.
            self._append_event({
                "type": "tool_call",
                "name": name,
                "input": input,
                "tool_use_id": tool_use_id,
            })
            if ext:
                # External hooks are 2-arg (pre-v2.0.19). Call with 2 positional
                # args to preserve that contract; integrators that want the id
                # should adopt the internal 3-arg form explicitly.
                ext(name, input)

        def get_count() -> int:
            return count[0]

        return on_tool_call, get_count

    def _make_tool_done_callback(self):
        """Return a composed on_tool_done callback.

        Emits a ``tool_done`` event to events.jsonl after each tool execution,
        giving the UI visibility into tool results. Composes with the external
        on_tool_done hook if set.

        When the result is a background-spawn placeholder (``"task_id=…"``), the
        event carries ``is_background=true`` plus the parsed ``tid`` so the
        frontend keeps the yellow "working" cell in place and waits for the
        matching ``tool_finalize`` event from ``_drain_background_events``.

        v2.0.19: emits ``duration_ms`` from the ``_tool_started`` timestamp the
        paired ``on_tool_call`` recorded — not the background-task wall clock,
        just the synchronous tool-executor duration. For backgrounded tools
        this reflects only the spawn time (placeholder returns near-instantly);
        the real runtime lands on ``tool_finalize``.
        """
        ext = self.on_tool_done
        import time as _time

        def on_tool_done(
            name: str,
            input: dict,
            result: str,
            tool_use_id: str,
            is_error: bool = False,
        ) -> None:
            # Cap the result text we ship through events.jsonl so huge tool
            # outputs (bash screenfuls, file reads) don't bloat the SSE
            # stream. Full output is still available via the Panel tab.
            _MAX = 8000
            result_str = result if isinstance(result, str) else str(result)
            truncated = len(result_str) > _MAX
            payload = {
                "type": "tool_done",
                "name": name,
                "result_len": len(result_str),
                "result": result_str[:_MAX],
                # v2.0.20: persisted so history replay can pair a reloaded
                # tool_use block back to its wall-clock duration_ms below.
                "tool_use_id": tool_use_id,
            }
            if truncated:
                payload["result_truncated"] = True
            # v2.0.23: classify-or-exception-derived error flag. Matches the
            # ``is_error`` already stored on the paired ``tool_result`` block in
            # context.jsonl (see core/agent.py::_execute_tools), so live UI and
            # history replay both key on the same bit to flip the cell red.
            if is_error:
                payload["is_error"] = True
            # v2.0.19 (parallel): per-call wall-clock for the HUD phase timer.
            # Paired with ``_tool_started[tool_use_id]`` from the on_tool_call
            # side so concurrent gather()'d calls don't mix up durations.
            started = self._tool_started.pop(tool_use_id, None)
            if started is not None:
                payload["duration_ms"] = int((_time.monotonic() - started) * 1000)
            tid = _parse_background_tid(result)
            if tid is not None:
                payload["is_background"] = True
                payload["tid"] = tid
            self._append_event(payload)
            # Newly-spawned sub_agent → bump HUD count immediately. Final
            # decrement happens in _drain_background_events when the runner
            # emits the terminal event.
            if name == "sub_agent" and tid is not None:
                self._emit_sub_agent_count()
            if ext:
                # External hooks are 3-arg (pre-v2.0.19) — preserve that contract.
                ext(name, input, result)

        return on_tool_done

    def _initial_input_offset(self) -> int:
        """Byte position in ``context.jsonl`` immediately after the last
        committed ``turn`` event, or 0 if no turn has been written yet.

        Used by ``run_daemon_loop`` to seed ``input_offset`` so:
          - Fresh sessions (no turns) rewind to 0 and pick up any
            ``user_input`` that was written by ``init_session`` before the
            daemon started.
          - Resumed sessions skip history already committed as turns; any
            ``user_input`` that arrived *after* the last turn (e.g. a mid-
            flight crash) is still replayed.
        """
        if not self._context_path.exists():
            return 0
        last_turn_end = 0
        try:
            with self._context_path.open("rb") as f:
                while True:
                    line_start = f.tell()
                    line = f.readline()
                    if not line:
                        break
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        evt = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("type") == "turn":
                        last_turn_end = f.tell()  # byte right after newline
        except OSError:
            return 0
        return last_turn_end

    def _emit_sub_agent_count(self) -> None:
        """Re-broadcast the running sub_agent tally as a HUD-side event."""
        from butterfly.session_engine.panel import (
            list_entries as _list_entries,
            TYPE_SUB_AGENT as _TYPE_SUB_AGENT,
        )
        running = sum(
            1 for e in _list_entries(self.panel_dir)
            if e.type == _TYPE_SUB_AGENT and not e.is_terminal()
        )
        self._append_event({
            "type": "sub_agent_count",
            "running": running,
        })

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

    def _make_llm_call_end_callback(self):
        """Return ``on_llm_call_end`` — writes ``llm_call_usage`` to events.jsonl.

        Fires once per completed ``provider.complete()`` inside Agent.run's
        loop. Carries the single call's usage + wall-clock duration so the HUD
        can compute:

          * ``context_tokens`` (= ``input + cache_read + cache_write + output``):
            the total tokens this call touched, which is a faithful proxy for
            "how full is the context window right now" — the next call's
            prompt is approximately this many tokens (cache_read already sat
            in the prompt; cache_write is fresh prompt; input is fresh prompt;
            output becomes the assistant turn that goes into the next prompt).
            ``reasoning_tokens`` is NOT added separately: per the OpenAI /
            Codex spec it is a subset of ``output_tokens``, so adding would
            double-count.

          * ``toks_per_s`` (= ``output_tokens * 1000 / duration_ms``): the
            LLM's output-generation speed for this call, shown on the HUD as
            a realtime latest-phase value (not an accumulator — each new
            call's value replaces the prior one).

        The event is written to events.jsonl (not context.jsonl) because it's
        HUD metadata, not a replayable turn component.
        """
        def on_llm_call_end(
            usage: "TokenUsage",
            duration_ms: int,
            iteration: int,
            tool_use_ids: list[str],
        ) -> None:
            ctx = (
                usage.input_tokens
                + usage.cache_read_tokens
                + usage.cache_write_tokens
                + usage.output_tokens
            )
            toks_per_s: float | None = None
            if duration_ms > 0 and usage.output_tokens > 0:
                toks_per_s = round(usage.output_tokens * 1000.0 / duration_ms, 2)
            payload = {
                "type": "llm_call_usage",
                "iteration": iteration,
                "duration_ms": duration_ms,
                "usage": usage.as_dict(),
                "context_tokens": ctx,
                "toks_per_s": toks_per_s,
            }
            self._append_event(payload)
            # v2.0.23 round-7: emit ``iteration_usage`` so the LIVE frontend
            # can stamp the token footer on this iteration's tool cells + the
            # streaming agent cell. History replay already gets the footer via
            # ``per_iteration_usages`` positional pairing in ipc.py — this
            # event is strictly for mid-run updates (nothing to persist to
            # context.jsonl). ``has_text`` tells the frontend whether to also
            # stamp the streaming agent cell; ``tool_use_ids`` targets tool
            # cells by id.
            self._append_event({
                "type": "iteration_usage",
                "iteration": iteration,
                "usage": usage.as_dict(),
                "tool_use_ids": list(tool_use_ids),
                "has_text": self._text_output_started_at is not None,
            })
            # v2.0.23 round-6: record this call's usage for positional pairing
            # with the turn's assistant messages (one per iteration, always).
            # ``turn.per_iteration_usages[i]`` pairs with the i-th assistant
            # message in turn.messages — the frontend uses it to render the
            # dim token footer at the bottom of every cell body (thinking,
            # tool, agent). Append BEFORE the text-output branch below so
            # we capture tool-only / thinking-only iterations too.
            self._current_turn_iteration_usages.append(usage.as_dict())
            # v2.0.20: if this LLM call produced any text output (the chunk
            # callback stamped a start timestamp), emit agent_output_done so
            # the UI can show "Output Xs" on both the live cell and history
            # replay. Stays silent for tool-only / thinking-only iterations.
            import time as _time_mod
            started_out = self._text_output_started_at
            if started_out is not None:
                output_duration_ms = int((_time_mod.monotonic() - started_out) * 1000)
                self._append_event({
                    "type": "agent_output_done",
                    "iteration": iteration,
                    "duration_ms": output_duration_ms,
                })
                self._text_output_started_at = None
                # Buffer the duration for the turn writer. One entry per
                # LLM call that produced text — same ordering as the text
                # content blocks that end up in the turn's messages.
                self._current_turn_agent_durations.append(output_duration_ms)
                # v2.0.23: same positional queue for per-call usage — one
                # entry per text-producing call, same ordering. Turn writer
                # drains onto ``turn["agent_output_usages"]`` so history +
                # live render each agent cell with its own call's tokens
                # instead of the cumulative turn total. Storing the dict
                # form (not TokenUsage) so it round-trips through JSON
                # without a custom decoder.
                self._current_turn_agent_usages.append(usage.as_dict())
            # Attribution for Part E — stamp the last thinking block of this
            # call with the provider-reported reasoning_tokens so the frontend
            # can render "Thought Xs for N tokens". The thinking-callback
            # factory exposes a setter when active; swallow if absent (not in
            # a thinking-enabled run, or no thinking block closed during this
            # iteration).
            attributor = getattr(self, "_pending_thinking_attributor", None)
            if attributor is not None:
                try:
                    attributor(usage.reasoning_tokens)
                except Exception:
                    _log.warning("thinking-token attributor raised", exc_info=True)

        return on_llm_call_end

    def _emit_version_notice_if_stale(self) -> None:
        """Emit system_notice if the meta session is at a newer version than this session."""
        manifest_path = self.system_dir / "manifest.json"
        if not manifest_path.exists():
            return
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return
        agent_name = manifest.get("agent", "")
        if not agent_name:
            return
        # Skip the meta session itself
        if self._session_id == f"{agent_name}_meta":
            return
        try:
            from butterfly.session_engine.agent_state import get_meta_version
            meta_version = get_meta_version(agent_name)
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

    def _make_thinking_callbacks(self):
        """Return ``(on_thinking_start, on_thinking_end, had_any, get_collected)``.

        Thinking blocks render as a dedicated tool-like cell in the web UI:
        ``on_thinking_start`` opens a cell showing "Thinking…" and
        ``on_thinking_end`` replaces it with the full body (collapsible).

        Each call allocates a fresh ``block_id`` so concurrent / sequential
        blocks pair correctly on the frontend. Duration is measured
        server-side and embedded in ``thinking_done`` so the UI does not
        depend on a wall clock that may have drifted between tabs.

        ``had_any()`` returns True if at least one thinking_done was emitted
        — used by the caller to mark the completed turn with
        ``has_streaming_thinking`` so history replay doesn't double-emit.

        ``get_collected()`` returns the list of ``{block_id, text, ts,
        duration_ms}`` dicts captured during the run — persisted onto the
        turn so history replay can restore the cells on re-entry, even for
        providers that don't roundtrip thinking text through message.content
        (codex reasoning items, Anthropic's non-text thinking blocks).

        v2.0.19 attribution: ``self._pending_thinking_attributor`` is set to
        a setter that ``_make_llm_call_end_callback`` calls with the
        reasoning_tokens reported by the provider for the LLM call that just
        finished. The setter emits a ``thinking_tokens_update`` event naming
        the last thinking block closed during that call so the frontend can
        flip the cell label from "Thought Xs" to "Thought Xs for N tokens".
        Anthropic reports ``reasoning_tokens=0``; the setter short-circuits
        on 0, so the frontend stays on "Thought Xs" and the docstring
        commitment (null for providers that don't expose per-call reasoning)
        is preserved.
        """
        import time as _time

        counter: list[int] = [0]
        pending: list[tuple[str, float]] = []  # stack of (block_id, started_at)
        any_closed: list[bool] = [False]
        collected: list[dict] = []
        # Block ids that closed since the last on_llm_call_end. Drained by the
        # attributor so each call's reasoning_tokens is credited to the
        # blocks that actually belong to it — not to blocks from a prior
        # iteration in the same turn.
        closed_this_call: list[str] = []

        def on_thinking_start() -> None:
            counter[0] += 1
            block_id = f"th:{int(_time.time() * 1000)}:{counter[0]}"
            pending.append((block_id, _time.time()))
            # v2.0.20: seed the collected list with a placeholder carrying
            # ``interrupted=True``. A normal on_thinking_end below upgrades
            # the entry in place (flag cleared, text + duration filled). If
            # the turn gets interrupted before on_thinking_end fires, the
            # placeholder survives in the persisted ``thinking_blocks``
            # list so history replay can render a "Thinking interrupted"
            # cell instead of silently dropping the block.
            collected.append({
                "block_id": block_id,
                "text": "",
                "ts": datetime.now().isoformat(),
                "interrupted": True,
            })
            self._append_event({"type": "thinking_start", "block_id": block_id})

        def on_thinking_end(text: str) -> None:
            if not pending:
                # Defensive — provider emitted end without start. Synthesize
                # a block_id so the event is still well-formed; the frontend
                # will treat it as an immediately-closed cell.
                counter[0] += 1
                block_id = f"th:{int(_time.time() * 1000)}:{counter[0]}"
                started_at = _time.time()
                synthesized = True
            else:
                block_id, started_at = pending.pop()
                synthesized = False
            duration_ms = int((_time.time() - started_at) * 1000)
            self._append_event({
                "type": "thinking_done",
                "block_id": block_id,
                "text": text or "",
                "duration_ms": duration_ms,
            })
            # Upgrade the placeholder seeded by on_thinking_start (clear the
            # interrupted flag, fill body + duration). Falls back to append
            # for the synthesized/defensive path so the persisted list still
            # has an entry even when the start callback never fired.
            upgraded = False
            if not synthesized:
                for entry in reversed(collected):
                    if entry.get("block_id") == block_id:
                        entry["text"] = text or ""
                        entry["duration_ms"] = duration_ms
                        entry["ts"] = datetime.now().isoformat()
                        entry.pop("interrupted", None)
                        upgraded = True
                        break
            if not upgraded:
                collected.append({
                    "block_id": block_id,
                    "text": text or "",
                    "duration_ms": duration_ms,
                    "ts": datetime.now().isoformat(),
                })
            closed_this_call.append(block_id)
            any_closed[0] = True

        def attribute_reasoning_tokens(reasoning_tokens: int) -> None:
            # Drain regardless of value so the NEXT call doesn't inherit
            # this call's closed-blocks list.
            blocks = list(closed_this_call)
            closed_this_call.clear()
            if reasoning_tokens <= 0 or not blocks:
                return
            # Attribute the entire call's reasoning total to the LAST block
            # closed during this call. Codex / Kimi typically emit a single
            # summary block per call, so this matches reality; for the rare
            # multi-block case the total stamps cleanly on the final one.
            target_id = blocks[-1]
            tokens = int(reasoning_tokens)
            self._append_event({
                "type": "thinking_tokens_update",
                "block_id": target_id,
                "reasoning_tokens": tokens,
            })
            # Mutate the collected entry too so the persisted turn data keeps
            # the attribution across reloads / history replay. Iterate in
            # reverse — the target is almost always the tail.
            for entry in reversed(collected):
                if entry.get("block_id") == target_id:
                    entry["reasoning_tokens"] = tokens
                    break

        self._pending_thinking_attributor = attribute_reasoning_tokens

        def had_any() -> bool:
            return any_closed[0]

        def get_collected() -> list[dict]:
            return list(collected)

        return on_thinking_start, on_thinking_end, had_any, get_collected

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
        import time as _time

        buf: list[str] = []
        buf_len: list[int] = [0]
        ext = self.on_text_chunk
        FLUSH_THRESHOLD = 150

        def on_chunk(chunk: str) -> None:
            # v2.0.20: stamp the "first chunk of this LLM call" timestamp so
            # on_llm_call_end can emit agent_output_done with the measured
            # streaming duration. Only the first chunk of each call sets it;
            # the callback resets back to None after emitting the event.
            # Paired with a lightweight ``agent_output_start`` event so the
            # frontend can open the "Typing…" cell immediately without
            # waiting for the 150-char buffer below to flush.
            if self._text_output_started_at is None and chunk:
                self._text_output_started_at = _time.monotonic()
                self._append_event({"type": "agent_output_start"})
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
