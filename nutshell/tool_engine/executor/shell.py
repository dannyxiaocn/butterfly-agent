from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from nutshell.tool_engine.executor.base import BaseExecutor


class ShellExecutor(BaseExecutor):
    """Executor for agent-created .sh shell script tools.

    Passes all tool kwargs as a JSON object on stdin.
    The script should write its result to stdout.
    """

    def __init__(self, sh_path: Path, cwd: str | None = None) -> None:
        self._sh_path = sh_path
        self._cwd = cwd

    @classmethod
    def can_handle(cls, tool_name: str, tool_path: Path | None) -> bool:
        if tool_path is None:
            return False
        sh = tool_path.with_suffix(".sh")
        return sh.exists()

    async def execute(self, **kwargs: Any) -> str:
        input_json = json.dumps(kwargs).encode()
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(self._sh_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_json),
                timeout=30.0,
            )
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace")
                return f"Error (exit {proc.returncode}): {err[:500]}"
            return stdout.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            return "Error: shell tool timed out after 30s"
        except Exception as e:
            return f"Error: {e}"
