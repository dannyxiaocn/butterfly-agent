"""Pure event-stream → provider-message transformer.

Phase 2 of web-ui-refactor. See `docs/refactor/DESIGN.md` §4.

Given the chronological event stream from `butterfly.runtime.events.read_events`,
`build_llm_context` filters to `for_llm=True` events and groups adjacent
same-role entries into provider-ready `Message` objects with typed content
blocks. No IO, no logging, no caching — purely functional.

Invariant I3 (LLM context is exactly `build_llm_context(read_events(id))`) and
I7 (live SSE and replay same payload) hinge on this module staying pure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Union

from butterfly.runtime.events import (
    EVENT_AGENT_TEXT,
    EVENT_AGENT_THINKING,
    EVENT_AGENT_TOOL_CALL,
    EVENT_AGENT_TOOL_RESULT,
    EVENT_USER_INPUT,
    EVENT_USER_INTERRUPT,
    Event,
)


# ── Block types (DESIGN.md §4.1) ──────────────────────────────────────────────

@dataclass(frozen=True)
class TextBlock:
    """A plain text content block."""

    text: str = ""
    type: str = "text"

    def to_dict(self) -> dict:
        return {"type": self.type, "text": self.text}


@dataclass(frozen=True)
class ThinkingBlock:
    """A reasoning / thinking content block.

    `signature` and `summary` are provider-specific optional fields. They are
    always present in the IR shape (as `None` when unset) so equality checks
    across live/replay do not diverge on missing keys.
    """

    text: str = ""
    signature: Union[str, None] = None
    summary: Union[str, None] = None
    redacted: bool = False
    type: str = "thinking"

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "text": self.text,
            "signature": self.signature,
            "summary": self.summary,
            "redacted": self.redacted,
        }


@dataclass(frozen=True)
class ToolUseBlock:
    """An assistant tool invocation block."""

    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    type: str = "tool_use"

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "id": self.id,
            "name": self.name,
            "input": dict(self.input),
        }


@dataclass(frozen=True)
class ToolResultBlock:
    """A tool-execution result, carried as a user-role block."""

    tool_use_id: str = ""
    content: str = ""
    is_error: bool = False
    type: str = "tool_result"

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "tool_use_id": self.tool_use_id,
            "content": self.content,
            "is_error": self.is_error,
        }


Block = Union[TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock]


@dataclass(frozen=True)
class Message:
    """One provider message: role + ordered content blocks."""

    role: str
    content: tuple[Block, ...]

    def to_dict(self) -> dict:
        return {"role": self.role, "content": [b.to_dict() for b in self.content]}


# ── Sentinel for "skip this event" in the converter table ─────────────────────

_SKIP = object()


# ── Per-event-type converters (DESIGN.md §4.2) ────────────────────────────────

def _convert_user_input(ev: Event) -> tuple[str, Block]:
    return ("user", TextBlock(text=str(ev.payload.get("text", ""))))


def _convert_user_interrupt(ev: Event):
    """Subtle: user_interrupt with text=None or empty string is a pure control
    signal and MUST NOT appear in the LLM context even though its taxonomy
    flag is for_llm=True. Only a non-empty `text` becomes a user turn.
    """
    text = ev.payload.get("text")
    if text is None or text == "":
        return _SKIP
    return ("user", TextBlock(text=str(text)))


def _convert_agent_text(ev: Event) -> tuple[str, Block]:
    return ("assistant", TextBlock(text=str(ev.payload.get("text", ""))))


def _convert_agent_thinking(ev: Event) -> tuple[str, Block]:
    p = ev.payload
    return (
        "assistant",
        ThinkingBlock(
            text=str(p.get("text", "")),
            signature=p.get("signature"),
            summary=p.get("summary"),
            redacted=bool(p.get("redacted", False)),
        ),
    )


def _convert_agent_tool_call(ev: Event) -> tuple[str, Block]:
    p = ev.payload
    return (
        "assistant",
        ToolUseBlock(
            id=str(p.get("tool_use_id", "")),
            name=str(p.get("tool_name", "")),
            input=dict(p.get("args") or {}),
        ),
    )


def _convert_agent_tool_result(ev: Event) -> tuple[str, Block]:
    p = ev.payload
    # Coerce result to string. The provider block type requires string content;
    # upstream tool executors may pass through ints/dicts/bytes. We stringify
    # with `str()` (Python repr for non-strings), matching what current code
    # has always sent on the wire.
    result = p.get("result", "")
    if isinstance(result, bytes):
        content = result.decode("utf-8", errors="replace")
    elif isinstance(result, str):
        content = result
    else:
        content = str(result)
    return (
        "user",
        ToolResultBlock(
            tool_use_id=str(p.get("tool_use_id", "")),
            content=content,
            is_error=bool(p.get("is_error", False)),
        ),
    )


# Exhaustive for every for_llm=True event type in DESIGN.md §3.3-§3.4. Any
# new for_llm type added upstream MUST be added here too; otherwise
# `build_llm_context` raises ValueError.
_CONVERTERS: dict[str, Callable[[Event], object]] = {
    EVENT_USER_INPUT: _convert_user_input,
    EVENT_USER_INTERRUPT: _convert_user_interrupt,
    EVENT_AGENT_TEXT: _convert_agent_text,
    EVENT_AGENT_THINKING: _convert_agent_thinking,
    EVENT_AGENT_TOOL_CALL: _convert_agent_tool_call,
    EVENT_AGENT_TOOL_RESULT: _convert_agent_tool_result,
}


# ── Main builder (DESIGN.md §4.1) ─────────────────────────────────────────────

def build_llm_context(events: Iterable[Event]) -> list[Message]:
    """Transform a chronological event stream into provider-ready messages.

    Purely functional. Given the same event sequence N times, returns the same
    messages. Invariants:

      - Only events with ``for_llm=True`` contribute.
      - Event order is preserved.
      - Adjacent same-role events merge into ONE message with content blocks
        in event order.
      - When the role switches, a new message starts.

    Subtle rules:
      - ``user_interrupt`` with ``text`` None or empty string is skipped
        entirely (pure control signal; not surfaced to the LLM).
      - An event with ``for_llm=True`` whose type is not in the converter
        table raises ``ValueError`` — schema integrity safeguard.
      - Events with ``for_llm=False`` are dropped silently.

    See DESIGN.md §4 for the complete contract.
    """
    messages: list[Message] = []
    current_role: str | None = None
    current_blocks: list[Block] = []

    def flush() -> None:
        nonlocal current_role, current_blocks
        if current_role is not None and current_blocks:
            messages.append(
                Message(role=current_role, content=tuple(current_blocks))
            )
        current_role = None
        current_blocks = []

    for ev in events:
        if not ev.for_llm:
            continue

        converter = _CONVERTERS.get(ev.type)
        if converter is None:
            raise ValueError(
                f"build_llm_context: unknown for_llm event type {ev.type!r}"
            )

        converted = converter(ev)
        if converted is _SKIP:
            continue

        role, block = converted  # type: ignore[misc]
        if current_role is None:
            current_role = role
            current_blocks = [block]
        elif role == current_role:
            current_blocks.append(block)
        else:
            flush()
            current_role = role
            current_blocks = [block]

    flush()
    return messages


# ── Convenience: read from disk + build ───────────────────────────────────────

def read_llm_context(session_dir: Path) -> list[Message]:
    """Read events from ``session_dir`` and transform to LLM context.

    Thin disk-backed wrapper for callers that don't already hold an event
    iterator. Kept out of :func:`build_llm_context` to preserve its purity.
    """
    from butterfly.runtime.events import read_events

    return build_llm_context(read_events(session_dir))
