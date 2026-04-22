"""Pure-context tools.

Tools in this series ingest raw byte streams (pty output, ANSI escapes,
control chars) and yield clean text for the model. See `base.py` for the
shared ANSI-stripping helper; the persistent terminal backend (shared by
``terminal_create`` + ``terminal_use``) lives in ``terminal``.
"""
from butterfly.tool_engine.executor.pure_context.base import strip_ansi  # noqa: F401
from butterfly.tool_engine.executor.pure_context.terminal import (  # noqa: F401
    EnvFingerprint,
    PtyShell,
    RunResult,
    TerminalExecutor,
)

__all__ = [
    "EnvFingerprint",
    "PtyShell",
    "RunResult",
    "TerminalExecutor",
    "strip_ansi",
]
