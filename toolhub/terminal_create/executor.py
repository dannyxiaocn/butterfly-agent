"""Toolhub entry point for ``terminal_create``.

The real implementation lives on ``TerminalExecutor`` in
``butterfly/tool_engine/executor/pure_context/terminal.py``. The tool
loader injects the session-scoped ``TerminalExecutor`` singleton and
invokes ``.create(**kwargs)`` directly — this module exists so the
generic toolhub discovery path (``_load_tool_schema`` +
``_load_executor_module``) finds a file at the expected location.
"""
from __future__ import annotations

from butterfly.tool_engine.executor.pure_context.terminal import (  # noqa: F401
    TerminalExecutor,
)

__all__ = ["TerminalExecutor"]
