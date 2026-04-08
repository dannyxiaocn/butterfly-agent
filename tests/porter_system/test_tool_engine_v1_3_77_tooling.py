from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nutshell.tool_engine.executor.terminal.shell_terminal import ShellExecutor
from nutshell.tool_engine.executor.web_search.tavily_web_search import _tavily_search


class _FakeProc:
    def __init__(self) -> None:
        self.returncode = None
        self.killed = False
        self.communicate_calls = 0

    async def communicate(self, input=None):
        self.communicate_calls += 1
        if self.killed:
            self.returncode = -9
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class ToolEngineUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_shell_executor_kills_timed_out_process(self) -> None:
        proc = _FakeProc()
        with TemporaryDirectory() as td:
            shell_path = Path(td) / "tool.sh"
            shell_path.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            executor = ShellExecutor(shell_path)

            async def _fake_create_subprocess_exec(*args, **kwargs):
                return proc

            async def _raise_timeout(awaitable, timeout):
                awaitable.close()
                raise asyncio.TimeoutError

            with patch("asyncio.create_subprocess_exec", side_effect=_fake_create_subprocess_exec), patch(
                "asyncio.wait_for",
                side_effect=_raise_timeout,
            ):
                result = await executor.execute(value="x")

        self.assertIn("timed out", result)
        self.assertTrue(proc.killed)
        self.assertGreaterEqual(proc.communicate_calls, 1)

    async def test_tavily_rejects_unimplemented_filters_explicitly(self) -> None:
        with patch.dict("os.environ", {"TAVILY_API_KEY": "demo"}, clear=False):
            result = await _tavily_search("query", country="US", freshness="day")

        self.assertIn("supports only 'query' and 'count'", result)
        self.assertIn("country", result)
        self.assertIn("freshness", result)
