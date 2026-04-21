"""Legacy import shim — real implementation moved to `pure_context/`.

The persistent session_shell executor now lives under the
`pure_context` series (pty-backed, ANSI-stripped) because "shell that
emits clean text from a dirty pty stream" is the first instance of that
family of tools. See `butterfly/tool_engine/executor/pure_context/
session_shell.py` for the implementation.
"""
from __future__ import annotations

from butterfly.tool_engine.executor.pure_context.session_shell import (  # noqa: F401
    PtyShell,
    RunResult,
    SessionShellExecutor,
)

__all__ = ["SessionShellExecutor", "PtyShell", "RunResult"]
