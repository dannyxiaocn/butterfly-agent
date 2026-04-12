from __future__ import annotations

import unittest
from typing import Optional
from unittest.mock import patch

from nutshell.core.agent import Agent
from nutshell.core.provider import Provider
from nutshell.core.skill import Skill
from nutshell.core.tool import _python_type_to_json_schema, tool
from nutshell.core.types import TokenUsage, ToolCall


class _SequenceProvider(Provider):
    def __init__(self, responses):
        self._responses = iter(responses)

    async def complete(
        self,
        messages,
        tools,
        system_prompt,
        model,
        *,
        on_text_chunk=None,
        cache_system_prefix="",
        cache_last_human_turn=False,
        thinking=False,
        thinking_budget=8000,
        thinking_effort="high",
    ):
        return next(self._responses)


class _FailingProvider(Provider):
    async def complete(
        self,
        messages,
        tools,
        system_prompt,
        model,
        *,
        on_text_chunk=None,
        cache_system_prefix="",
        cache_last_human_turn=False,
        thinking=False,
        thinking_budget=8000,
        thinking_effort="high",
    ):
        raise RuntimeError("primary failure")


class CoreAgentToolTest(unittest.IsolatedAsyncioTestCase):
    def test_python_type_to_json_schema_handles_optional(self) -> None:
        self.assertEqual(_python_type_to_json_schema(Optional[int]), {"type": "integer"})

    def test_tool_decorator_builds_required_fields(self) -> None:
        @tool(description="Add numbers")
        def add(a: int, b: int, label: str = "sum") -> int:
            return a + b

        self.assertEqual(add.schema["required"], ["a", "b"])
        self.assertEqual(add.schema["properties"]["label"], {"type": "string"})

    def test_memory_layer_truncation_mentions_source_file(self) -> None:
        content = "\n".join(f"line {idx}" for idx in range(100))
        rendered = Agent._render_memory_layer("notes", content)
        self.assertIn("cat core/memory/notes.md", rendered)
        self.assertIn("lines omitted", rendered)

    def test_build_system_parts_includes_agent_mode_guidance(self) -> None:
        agent = Agent(
            system_prompt="base prompt",
            skills=[Skill(name="inline", description="desc", body="body")],
        )
        agent.memory = "remember this"
        agent.app_notifications = [("mail", "1 unread")]
        agent.caller_type = "agent"
        static, dynamic = agent._build_system_parts()
        self.assertIn("base prompt", static)
        self.assertIn("Session Memory", dynamic)
        self.assertIn("App Notifications", dynamic)
        self.assertIn("Agent Collaboration Mode", dynamic)
        self.assertIn("Skill: inline", dynamic)

    async def test_agent_executes_tool_loop(self) -> None:
        call = ToolCall(id="1", name="add", input={"a": 2, "b": 3})

        @tool(description="Add")
        def add(a: int, b: int) -> int:
            return a + b

        agent = Agent(
            provider=_SequenceProvider(
                [
                    ("", [call], TokenUsage()),
                    ("done", [], TokenUsage(output_tokens=5)),
                ]
            ),
            tools=[add],
        )
        result = await agent.run("sum")
        self.assertEqual(result.content, "done")
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.iterations, 2)

    async def test_agent_uses_fallback_provider_after_primary_failure(self) -> None:
        fallback = _SequenceProvider([("recovered", [], TokenUsage(output_tokens=3))])
        with patch("nutshell.llm_engine.registry.resolve_provider", return_value=fallback):
            agent = Agent(
                provider=_FailingProvider(),
                fallback_provider="openai",
                fallback_model="gpt-fallback",
            )
            result = await agent.run("hello")
        self.assertEqual(result.content, "recovered")


if __name__ == "__main__":
    unittest.main()
