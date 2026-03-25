from __future__ import annotations
import asyncio
from abc import ABC, abstractmethod
from typing import Any, Callable, Literal

from nutshell.core.provider import Provider
from nutshell.core.skill import Skill
from nutshell.core.tool import Tool
from nutshell.core.types import AgentResult, Message, ToolCall

class BaseAgent(ABC):
    """Abstract interface for an agent that processes messages."""

    @abstractmethod
    async def run(self, input: str, *, clear_history: bool = False) -> "AgentResult":
        """Run the agent with a user input string and return a result."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Release any held state (e.g., conversation history)."""
        ...


ReleasePolicy = Literal["auto", "manual", "persistent"]

_DEFAULT_MODEL = "claude-sonnet-4-6"


class Agent(BaseAgent):
    """A minimal LLM agent.

    Args:
        system_prompt: Defines the agent's identity and behavior.
        tools: List of Tool objects the agent can call.
        skills: List of Skill objects. File-backed skills (with a ``location``)
                are listed in a catalog so the model can activate them on
                demand (progressive disclosure). Inline skills (no location)
                have their body injected directly into the system prompt.
        model: Model identifier string (default: claude-sonnet-4-6).
        provider: LLM provider instance. If omitted, AnthropicProvider
                  is used with the ANTHROPIC_API_KEY environment variable.
        release_policy: Lifecycle when used as a sub-agent.
            "auto"       - history cleared after each parent run
            "manual"     - cleared only when .close() is called
            "persistent" - history preserved across runs
        max_iterations: Max tool-call loops per run (default: 20).
    """

    def __init__(
        self,
        system_prompt: str = "",
        tools: list[Tool] | None = None,
        skills: list[Skill] | None = None,
        model: str = _DEFAULT_MODEL,
        provider: Provider | None = None,
        release_policy: ReleasePolicy = "persistent",
        max_iterations: int = 20,
        heartbeat_prompt: str = "",
        session_context_template: str = "",
    ) -> None:
        self.system_prompt = system_prompt
        self.tools: list[Tool] = tools or []
        self.skills: list[Skill] = skills or []
        self.model = model
        self.release_policy = release_policy
        self.max_iterations = max_iterations
        self.heartbeat_prompt = heartbeat_prompt
        self.session_context_template = session_context_template
        self._provider = provider
        self._history: list[Message] = []
        # Runtime-injectable fields — set by Session before each activation.
        # Not constructor params; Session owns the values, Agent owns the rendering.
        self.memory: str = ""
        # Extra named memory layers from core/memory/*.md, sorted by filename.
        # Each entry is (label, content) where label is the .md file stem.
        self.memory_layers: list[tuple[str, str]] = []
        self.session_context: str = ""

    @property
    def provider(self) -> Provider:
        if self._provider is None:
            from nutshell.llm_engine.providers.anthropic import AnthropicProvider
            self._provider = AnthropicProvider()
        return self._provider

    def _build_system_parts(self) -> tuple[str, str]:
        """Return (static_prefix, dynamic_suffix) for cache-aware prompt building.

        static_prefix  — system.md + session context. Stable across activations;
                         eligible for Anthropic prompt caching.
        dynamic_suffix — memory + skills. Changes each activation; not cached.
        """
        from nutshell.skill_engine.renderer import build_skills_block
        static_parts = [self.system_prompt] if self.system_prompt else []
        if self.session_context:
            static_parts.append("\n\n---\n" + self.session_context)

        dynamic_parts: list[str] = []
        if self.memory or self.memory_layers:
            memory_parts = []
            if self.memory:
                memory_parts.append(f"## Session Memory\n\n{self.memory}")
            for name, content in self.memory_layers:
                memory_parts.append(f"## Memory: {name}\n\n{content}")
            dynamic_parts.append("\n\n---\n" + "\n\n".join(memory_parts))
        skills_block = build_skills_block(self.skills)
        if skills_block:
            dynamic_parts.append(skills_block)

        return "\n".join(static_parts), "\n".join(dynamic_parts)

    def _build_system_prompt(self) -> str:
        """Return full system prompt as a single string (backward-compatible)."""
        prefix, suffix = self._build_system_parts()
        parts = [p for p in [prefix, suffix] if p]
        return "\n".join(parts)

    def _tool_map(self) -> dict[str, Tool]:
        return {t.name: t for t in self.tools}

    async def run(
        self,
        input: str,
        *,
        clear_history: bool = False,
        on_text_chunk: Callable[[str], None] | None = None,
        on_tool_call: Callable[[str, dict], None] | None = None,
    ) -> AgentResult:
        """Run the agent with the given input and return an AgentResult.

        Args:
            input: The user message to send.
            clear_history: If True, clears conversation history before this run.
        """
        if clear_history:
            self._history = []

        system_prefix, system_dynamic = self._build_system_parts()
        tool_map = self._tool_map()
        messages: list[Message] = [*self._history, Message(role="user", content=input)]
        all_tool_calls: list[ToolCall] = []

        for _ in range(self.max_iterations):
            content, tool_calls = await self.provider.complete(
                messages=messages,
                tools=self.tools,
                system_prompt=system_dynamic,
                model=self.model,
                on_text_chunk=on_text_chunk,
                cache_system_prefix=system_prefix,
            )
            # Only stream the first completion; subsequent rounds (tool loops)
            # don't stream since the user only cares about the final text.
            on_text_chunk = None

            # Build assistant message content for Anthropic format
            assistant_content: Any = content
            if tool_calls:
                # Anthropic expects content blocks for tool_use
                blocks: list[Any] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in tool_calls:
                    blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
                assistant_content = blocks

            messages.append(Message(role="assistant", content=assistant_content))
            all_tool_calls.extend(tool_calls)

            if not tool_calls:
                break

            # Notify about each tool call before executing (for real-time streaming)
            if on_tool_call:
                for tc in tool_calls:
                    on_tool_call(tc.name, tc.input)

            # Execute tools and append results
            tool_results = await _execute_tools(tool_calls, tool_map)
            messages.append(Message(role="tool", content=tool_results))

        # Update history
        self._history = list(messages)

        result = AgentResult(
            content=content,
            tool_calls=all_tool_calls,
            messages=list(messages),
        )

        if self.release_policy == "auto":
            self._history = []

        return result

    def close(self) -> None:
        """Clear conversation history (for release_policy='manual')."""
        self._history = []

    def as_tool(
        self,
        name: str,
        description: str,
        *,
        clear_history: bool = False,
    ) -> Tool:
        """Wrap this agent as a Tool that can be used by another agent.

        The sub-agent receives the tool input as its user message.

        Args:
            clear_history: If True, clears the sub-agent history before each tool
                invocation. Useful when you want a normally persistent agent to act
                like a stateless worker in a multi-agent pipeline.
        """
        agent = self

        async def _run(input: str) -> str:
            result = await agent.run(input, clear_history=clear_history)
            if agent.release_policy == "auto":
                agent.close()
            return result.content

        _run.__doc__ = description
        return Tool(
            name=name,
            description=description,
            func=_run,
            schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        )


async def _execute_tools(
    tool_calls: list[ToolCall],
    tool_map: dict[str, Tool],
) -> list[dict]:
    """Execute tool calls concurrently and return Anthropic-format tool_result blocks."""
    async def _call(tc: ToolCall) -> dict:
        tool = tool_map.get(tc.name)
        if tool is None:
            content = f"Error: tool '{tc.name}' not found."
        else:
            try:
                content = await tool.execute(**tc.input)
            except Exception as exc:
                content = f"Error executing '{tc.name}': {exc}"
        return {"type": "tool_result", "tool_use_id": tc.id, "content": content}

    return list(await asyncio.gather(*[_call(tc) for tc in tool_calls]))
