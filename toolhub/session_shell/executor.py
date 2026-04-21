"""Toolhub entry point for the `session_shell` tool.

The real implementation lives under the pure_context series:
`butterfly/tool_engine/executor/pure_context/session_shell.py`. This
module re-exports `SessionShellExecutor` so `ToolLoader._load_executor_module`
can import it via the conventional `toolhub/<name>/executor.py` path.
"""
from __future__ import annotations

from butterfly.tool_engine.executor.pure_context.session_shell import (  # noqa: F401
    SessionShellExecutor,
)

__all__ = ["SessionShellExecutor"]
