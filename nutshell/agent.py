from __future__ import annotations
import asyncio
from typing import Any, Literal

from nutshell.base.agent import BaseAgent
from nutshell.provider import Provider
from nutshell.skill import Skill
from nutshell.tool import Tool
from nutshell.types import AgentResult, Message, ToolCall

ReleasePolicy = Literal["auto", "manual", "persistent"]

_DEFAULT_MODEL = "claude-sonnet-4-6"


class Agent(BaseAgent):
    """A minimal LLM agent.

    Args:
        system_prompt: Defines the agent's identity and behavior.
        tools: List of Tool objects the agent can call.
        skills: List of Skill objects whose prompt_injection is appended
                to the system prompt.
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
    ) -> None:
        self.system_prompt = system_prompt
        self.tools: list[Tool] = tools or []
        self.skills: list[Skill] = skills or []
        self.model = model
        self.release_policy = release_policy
        self.max_iterations = max_iterations
        self._provider = provider
        self._history: list[Message] = []

    @property
    def provider(self) -> Provider:
        if self._provider is None:
            from nutshell.providers.anthropic import AnthropicProvider
            self._provider = AnthropicProvider()
        return self._provider

    def _build_system_prompt(self) -> str:
        parts = [self.system_prompt] if self.system_prompt else []
        for skill in self.skills:
            parts.append(f"\n\n# Skill: {skill.name}\n{skill.prompt_injection}")
        return "\n".join(parts)

    def _tool_map(self) -> dict[str, Tool]:
        return {t.name: t for t in self.tools}

    async def run(self, input: str, *, clear_history: bool = False) -> AgentResult:
        """Run the agent with the given input and return an AgentResult.

        Args:
            input: The user message to send.
            clear_history: If True, clears conversation history before this run.
        """
        if clear_history:
            self._history = []

        system = self._build_system_prompt()
        tool_map = self._tool_map()
        messages: list[Message] = [*self._history, Message(role="user", content=input)]
        all_tool_calls: list[ToolCall] = []

        for _ in range(self.max_iterations):
            content, tool_calls = await self.provider.complete(
                messages=messages,
                tools=self.tools,
                system_prompt=system,
                model=self.model,
            )

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

    def as_tool(self, name: str, description: str) -> Tool:
        """Wrap this agent as a Tool that can be used by another agent.

        The sub-agent receives the tool input as its user message.
        """
        agent = self

        async def _run(input: str) -> str:
            result = await agent.run(input)
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
