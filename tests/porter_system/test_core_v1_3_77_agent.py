from __future__ import annotations

import unittest

from nutshell.core.agent import Agent, _execute_tools
from nutshell.core.provider import Provider
from nutshell.core.tool import Tool
from nutshell.core.types import TokenUsage, ToolCall


class _FakeProvider(Provider):
    async def complete(self, messages, tools, system_prompt, model, **kwargs):
        return "done", [], TokenUsage(output_tokens=1)


class AgentUnitTests(unittest.IsolatedAsyncioTestCase):
    def test_max_iterations_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            Agent(max_iterations=0)

    async def test_agent_runs_single_iteration(self) -> None:
        agent = Agent(provider=_FakeProvider(), model="demo", max_iterations=1)
        result = await agent.run("hello")

        self.assertEqual(result.content, "done")
        self.assertEqual(result.iterations, 1)
        self.assertEqual(result.messages[-1].role, "assistant")

    async def test_execute_tools_marks_errors(self) -> None:
        async def _boom(**kwargs):
            raise RuntimeError("boom")

        tool_map = {"broken": Tool(name="broken", description="broken", func=_boom)}
        results = await _execute_tools(
            [
                ToolCall(id="1", name="missing", input={}),
                ToolCall(id="2", name="broken", input={}),
            ],
            tool_map,
        )

        self.assertTrue(results[0]["is_error"])
        self.assertIn("not found", results[0]["content"])
        self.assertTrue(results[1]["is_error"])
        self.assertIn("boom", results[1]["content"])

    def test_render_memory_layer_truncates_large_content(self) -> None:
        content = "\n".join(f"line {i}" for i in range(65))
        rendered = Agent._render_memory_layer("notes", content)

        self.assertIn("Memory: notes", rendered)
        self.assertIn("full content: `cat core/memory/notes.md`", rendered)
        self.assertIn("5 lines omitted", rendered)

