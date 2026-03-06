from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class Message:
    role: Literal["user", "assistant", "tool"]
    content: str | list[Any]


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class AgentResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
