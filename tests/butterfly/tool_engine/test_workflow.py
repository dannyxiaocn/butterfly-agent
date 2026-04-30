"""Unit tests for workflow input validation + log formatting + sync execution."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from butterfly.tool_engine.workflow import (
    WorkflowExecutor, _format_full_log, _format_step_header, _validate_steps,
)


class ValidateStepsTests(unittest.TestCase):
    def test_empty_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty"):
            _validate_steps([])

    def test_non_list_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty"):
            _validate_steps("nope")

    def test_missing_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "name"):
            _validate_steps([{"task": "do it"}])

    def test_missing_task(self) -> None:
        with self.assertRaisesRegex(ValueError, "task"):
            _validate_steps([{"name": "step1"}])

    def test_invalid_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "mode"):
            _validate_steps([{"name": "s", "task": "t", "mode": "wild"}])

    def test_default_mode_executor(self) -> None:
        out = _validate_steps([{"name": "s", "task": "t"}])
        self.assertEqual(out[0]["mode"], "executor")

    def test_optional_agent_string(self) -> None:
        out = _validate_steps([{"name": "s", "task": "t", "agent": "agent"}])
        self.assertEqual(out[0]["agent"], "agent")
        with self.assertRaisesRegex(ValueError, "agent"):
            _validate_steps([{"name": "s", "task": "t", "agent": 7}])


class FormatTests(unittest.TestCase):
    def test_format_step_header_with_agent(self) -> None:
        h = _format_step_header(
            2, {"name": "draft", "agent": "writer", "mode": "executor"}
        )
        self.assertIn("step 2", h)
        self.assertIn("draft", h)
        self.assertIn("agent=writer", h)
        self.assertIn("mode=executor", h)

    def test_format_full_log_separator(self) -> None:
        rendered = [
            ({"name": "a", "agent": None, "mode": "executor"}, "out1"),
            ({"name": "b", "agent": None, "mode": "executor"}, "out2"),
        ]
        log = _format_full_log(rendered)
        self.assertIn("step 1", log)
        self.assertIn("step 2", log)
        self.assertIn("---", log)
        self.assertIn("out1", log)
        self.assertIn("out2", log)


class WorkflowExecutorSyncTests(unittest.TestCase):
    def test_steps_run_sequentially_with_prev_substitution(self) -> None:
        captured: list[dict] = []

        async def fake_execute(self, **kwargs):
            captured.append(dict(kwargs))
            return f"reply-for-{kwargs['name']}"

        executor = WorkflowExecutor(
            parent_session_id="parent-1",
            sessions_base="/tmp/sessions",
            system_sessions_base="/tmp/_sessions",
            agent_base="/tmp/agenthub",
        )
        steps = [
            {"name": "step1", "task": "do A"},
            {"name": "step2", "task": "do B with {prev}", "agent": "writer"},
            {"name": "step3", "task": "wrap {prev}", "mode": "explorer"},
        ]
        with patch(
            "butterfly.tool_engine.workflow.SubAgentTool.execute",
            new=fake_execute,
        ):
            result = asyncio.run(executor.execute(steps=steps))

        # 3 sub-agent invocations, sequenced
        self.assertEqual(len(captured), 3)
        # Step 1 has no prev → substitution gives empty string
        self.assertEqual(captured[0]["task"], "do A")
        # Step 2 saw step 1's reply substituted in for {prev}
        self.assertIn("reply-for-step1", captured[1]["task"])
        # agent_name forwarded only when set
        self.assertEqual(captured[1].get("agent_name"), "writer")
        self.assertNotIn("agent_name", captured[0])
        # Step 3 picked up step 2's reply, mode forwarded
        self.assertIn("reply-for-step2", captured[2]["task"])
        self.assertEqual(captured[2]["mode"], "explorer")
        # Aggregate log includes every step's reply
        for label in ("reply-for-step1", "reply-for-step2", "reply-for-step3"):
            self.assertIn(label, result)

    def test_validation_error_returns_string(self) -> None:
        executor = WorkflowExecutor(
            parent_session_id="parent-1",
            sessions_base="/tmp/sessions",
            system_sessions_base="/tmp/_sessions",
            agent_base="/tmp/agenthub",
        )
        result = asyncio.run(executor.execute(steps=[]))
        self.assertTrue(result.startswith("Error:"))


if __name__ == "__main__":
    unittest.main()
