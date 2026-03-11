"""Nutshell — a minimal Python Agent library."""

from nutshell.core.agent import Agent
from nutshell.runtime.session import Session, SESSION_FINISHED
from nutshell.runtime.ipc import FileIPC
from nutshell.abstract.provider import Provider
from nutshell.llm.anthropic import AnthropicProvider
from nutshell.core.skill import Skill
from nutshell.core.tool import Tool, tool
from nutshell.core.types import AgentResult, Message, ToolCall

# Abstract base classes
from nutshell.abstract.agent import BaseAgent
from nutshell.abstract.tool import BaseTool
from nutshell.abstract.loader import BaseLoader

# Loaders
from nutshell.runtime.loaders.tool import ToolLoader
from nutshell.runtime.loaders.skill import SkillLoader
from nutshell.runtime.loaders.agent import AgentLoader

# Built-in tools
from nutshell.runtime.tools import create_bash_tool

__all__ = [
    # Core
    "Agent",
    "Session",
    "SESSION_FINISHED",
    "FileIPC",
    "Provider",
    "AnthropicProvider",
    "Skill",
    "Tool",
    "tool",
    "AgentResult",
    "Message",
    "ToolCall",
    # Abstract base classes
    "BaseAgent",
    "BaseTool",
    "BaseLoader",
    # Loaders
    "ToolLoader",
    "SkillLoader",
    "AgentLoader",
    # Built-in tools
    "create_bash_tool",
]
