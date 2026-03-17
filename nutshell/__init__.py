"""Nutshell — a minimal Python Agent library."""

from nutshell.core.agent import Agent, BaseAgent
from nutshell.core.tool import Tool, BaseTool, tool
from nutshell.core.skill import Skill
from nutshell.core.types import AgentResult, Message, ToolCall
from nutshell.providers import Provider
from nutshell.providers.llm.anthropic import AnthropicProvider
from nutshell.runtime.loaders import BaseLoader
from nutshell.runtime.session import Session, SESSION_FINISHED
from nutshell.runtime.ipc import FileIPC
from nutshell.runtime.loaders.tool import ToolLoader
from nutshell.runtime.loaders.skill import SkillLoader
from nutshell.runtime.loaders.agent import AgentLoader
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
