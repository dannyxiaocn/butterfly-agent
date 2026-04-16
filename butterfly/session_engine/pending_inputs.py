"""Pending input queue items for the Session daemon dispatcher.

The dispatcher in ``Session.run_daemon_loop`` reads ``user_input`` events
from ``context.jsonl``, ``BackgroundTaskManager`` events from in-memory queues,
and due ``TaskCard`` activations, then produces queue items that are processed
sequentially with two modes:

* ``interrupt`` — cancel the currently-running agent loop and run this input
  next. If the cancelled run had not yet committed any assistant turn (i.e.
  the LLM never returned a response), the cancelled content is merged with
  the new content into a single user message — sending two consecutive user
  messages would otherwise violate the Anthropic / OpenAI message-ordering
  contract.

* ``wait`` — queue and run after any in-flight or earlier-queued items.
  Consecutive ``wait`` chat items at the queue tail are merged into a single
  user message so the agent only fires once for a burst of small follow-ups.

Default modes by source (set by the producers):

* ``user``  — chat / API send_message → ``interrupt``
* ``panel`` — background-task notifications → ``interrupt``
* ``task``  — task-card wakeups → ``wait``

``TaskItem`` wraps a ``TaskCard`` and is dispatched via ``Session.tick()``
instead of ``Session.chat()`` because task wakeups have their own prompt
template and history bookkeeping (mark_working / mark_finished /
``SESSION_FINISHED`` rollback). Task items never merge — each card runs in
its own activation.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from butterfly.session_engine.task_cards import TaskCard


# ── Modes ─────────────────────────────────────────────────────────────────────

MODE_INTERRUPT = "interrupt"
MODE_WAIT = "wait"

VALID_MODES = (MODE_INTERRUPT, MODE_WAIT)


def default_mode_for_source(source: str) -> str:
    """Default queue mode for an input source."""
    if source == "task":
        return MODE_WAIT
    if source == "panel":
        return MODE_INTERRUPT
    return MODE_INTERRUPT


# ── Items ─────────────────────────────────────────────────────────────────────


def merge_chat_content(prior: str, new: str) -> str:
    """Concatenate two chat messages with a blank-line separator."""
    if not prior:
        return new
    if not new:
        return prior
    return f"{prior}\n\n{new}"


@dataclass
class ChatItem:
    """A queued chat input destined for ``Session.chat()``.

    Fields:
        content: User-visible text. Mutated in place when items merge.
        mode: ``interrupt`` or ``wait``.
        source: ``user`` / ``panel`` / ``task`` — for telemetry only.
        caller_type: ``human`` / ``agent`` / ``system`` — passed through to
            ``Agent.run(caller_type=...)`` so the system prompt can adapt.
        user_input_ids: Every msg_id that was merged into this item. The
            written turn carries the **last** id (matches the most recent
            send), but all ids are tracked so wait-merge bookkeeping can
            resolve every caller's future.
        futures: One future per ``Session.chat()`` caller awaiting this item.
            When the item runs (or merges into another item that runs), the
            future is resolved with the resulting ``AgentResult``.
        sources: Provenance tags accumulated across merges (used by tests).
    """

    content: str
    mode: str
    source: str = "user"
    caller_type: str = "human"
    user_input_ids: list[str] = field(default_factory=list)
    futures: list[asyncio.Future] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(f"invalid mode: {self.mode!r}")
        if self.source and self.source not in self.sources:
            self.sources.append(self.source)

    @property
    def latest_user_input_id(self) -> Optional[str]:
        return self.user_input_ids[-1] if self.user_input_ids else None

    def merge_after(self, other: "ChatItem") -> None:
        """Append ``other``'s content/futures/ids onto this item.

        Ordering: ``self`` is the existing/older item, ``other`` is the new
        arrival being absorbed. Content is concatenated ``self + other``.
        Futures/ids are extended in arrival order.
        """
        self.content = merge_chat_content(self.content, other.content)
        self.user_input_ids.extend(other.user_input_ids)
        self.futures.extend(other.futures)
        for s in other.sources:
            if s not in self.sources:
                self.sources.append(s)
        # caller_type promotion: if any contributor was human, treat the
        # merged item as human-driven (the human wins over system/agent).
        if other.caller_type == "human":
            self.caller_type = "human"

    def merge_before(self, other: "ChatItem") -> None:
        """Prepend ``other``'s content onto this item (used when an
        uncommitted run is interrupted — the cancelled content gets put
        back in front of the interrupting input)."""
        self.content = merge_chat_content(other.content, self.content)
        # Prepend ids/sources so latest_user_input_id remains ours
        self.user_input_ids[:0] = other.user_input_ids
        # Futures from the prior run still need resolution
        self.futures[:0] = other.futures
        for s in reversed(other.sources):
            if s not in self.sources:
                self.sources.insert(0, s)
        if other.caller_type == "human":
            self.caller_type = "human"

    def resolve(self, result) -> None:
        """Resolve every awaiting future with ``result``."""
        for fut in self.futures:
            if not fut.done():
                fut.set_result(result)
        self.futures.clear()

    def reject(self, exc: BaseException) -> None:
        """Fail every awaiting future with ``exc``."""
        for fut in self.futures:
            if not fut.done():
                fut.set_exception(exc)
        self.futures.clear()


@dataclass
class TaskItem:
    """A queued task-card wakeup destined for ``Session.tick()``.

    Always runs in wait mode — task wakeups never preempt active runs and
    never merge with chat items (the prompt template and the mark_working /
    mark_finished bookkeeping are card-specific).
    """

    card: "TaskCard"
    futures: list[asyncio.Future] = field(default_factory=list)

    @property
    def mode(self) -> str:
        return MODE_WAIT

    def resolve(self, result) -> None:
        for fut in self.futures:
            if not fut.done():
                fut.set_result(result)
        self.futures.clear()

    def reject(self, exc: BaseException) -> None:
        for fut in self.futures:
            if not fut.done():
                fut.set_exception(exc)
        self.futures.clear()
