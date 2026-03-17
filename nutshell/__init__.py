"""Nutshell — a minimal Python Agent library."""

from nutshell.core.agent import Agent, BaseAgent
from nutshell.core.tool import Tool, BaseTool, tool
from nutshell.core.skill import Skill
from nutshell.core.types import AgentResult, Message, ToolCall
from nutshell.core.provider import Provider
from nutshell.llm_engine.providers.anthropic import AnthropicProvider
from nutshell.core.loader import BaseLoader
from nutshell.runtime.session import Session, SESSION_FINISHED
from nutshell.runtime.ipc import FileIPC
from nutshell.tool_engine.loader import ToolLoader
from nutshell.skill_engine.loader import SkillLoader
from nutshell.skill_engine.renderer import build_skills_block
from nutshell.llm_engine.loader import AgentLoader
from nutshell.tool_engine.executor.bash import create_bash_tool

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
    "build_skills_block",
    "AgentLoader",
    # Built-in tools
    "create_bash_tool",
]
