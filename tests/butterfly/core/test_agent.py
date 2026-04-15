from __future__ import annotations

import unittest

from butterfly.core.agent import Agent, _execute_tools
from butterfly.core.provider import Provider
from butterfly.core.tool import Tool
from butterfly.core.types import TokenUsage, ToolCall


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



# ── BUG-3 regression: fallback_model-only path reuses primary provider ──


class FallbackProviderResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_fallback_configured_returns_none(self) -> None:
        agent = Agent(provider=_FakeProvider())
        self.assertIsNone(agent._get_fallback_provider())

    async def test_fallback_model_only_reuses_primary_provider(self) -> None:
        primary = _FakeProvider()
        agent = Agent(provider=primary, fallback_model="different-model")
        fb = agent._get_fallback_provider()
        self.assertIs(fb, primary)

    async def test_fallback_model_only_triggers_retry_with_new_model(self) -> None:
        """Primary fails once — loop must retry with the same provider but the fallback model."""
        calls: list[tuple[str, str]] = []

        class _FlakyProvider(Provider):
            async def complete(self, messages, tools, system_prompt, model, **kwargs):
                calls.append((type(self).__name__, model))
                if model == "primary-model":
                    raise RuntimeError("boom")
                return "ok", [], TokenUsage(output_tokens=1)

        agent = Agent(
            provider=_FlakyProvider(),
            model="primary-model",
            fallback_model="fallback-model",
        )
        result = await agent.run("hi")

        self.assertEqual(result.content, "ok")
        self.assertEqual(
            [model for _, model in calls],
            ["primary-model", "fallback-model"],
        )
