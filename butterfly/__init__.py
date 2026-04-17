"""Butterfly — a minimal Python Agent library."""

from butterfly.core.agent import Agent
from butterfly.core.tool import Tool, tool
from butterfly.core.skill import Skill
from butterfly.core.types import AgentResult, Message, ToolCall
from butterfly.core.provider import Provider
from butterfly.llm_engine.providers.anthropic import AnthropicProvider
from butterfly.core.loader import BaseLoader
from butterfly.session_engine.agent_config import AgentConfig
from butterfly.session_engine.session import Session, SESSION_FINISHED
from butterfly.runtime.ipc import FileIPC
from butterfly.tool_engine.loader import ToolLoader
from butterfly.skill_engine.loader import SkillLoader
from butterfly.skill_engine.renderer import build_skills_block
from butterfly.session_engine.agent_loader import AgentLoader
from butterfly.tool_engine.executor.terminal.bash_terminal import create_bash_tool

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
    # Config
    "AgentConfig",
    # Loaders
    "ToolLoader",
    "SkillLoader",
    "build_skills_block",
    "AgentLoader",
    # Built-in tools
    "create_bash_tool",
]
