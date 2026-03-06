"""Nutshell — a minimal Python Agent library."""

from nutshell.agent import Agent
from nutshell.provider import Provider
from nutshell.providers.anthropic import AnthropicProvider
from nutshell.skill import Skill
from nutshell.tool import Tool, tool
from nutshell.types import AgentResult, Message, ToolCall

# Abstract base classes (Layer 1)
from nutshell.base import BaseAgent, BaseTool, BaseSkill, BaseLoader

# External file loaders (Layer 1)
from nutshell.loaders import PromptLoader, ToolLoader, SkillLoader

__all__ = [
    # Core
    "Agent",
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
    "BaseSkill",
    "BaseLoader",
    # Loaders
    "PromptLoader",
    "ToolLoader",
    "SkillLoader",
]
