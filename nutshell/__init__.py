"""Nutshell — a minimal Python Agent library."""

from nutshell.agent import Agent
from nutshell.provider import Provider
from nutshell.providers.anthropic import AnthropicProvider
from nutshell.skill import Skill
from nutshell.tool import Tool, tool
from nutshell.types import AgentResult, Message, ToolCall

__all__ = [
    "Agent",
    "Provider",
    "AnthropicProvider",
    "Skill",
    "Tool",
    "tool",
    "AgentResult",
    "Message",
    "ToolCall",
]
