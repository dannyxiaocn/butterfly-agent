"""Pure-context tools.

Tools in this series ingest raw byte streams (pty output, ANSI escapes,
control chars) and yield clean text for the model. See `base.py` for the
shared ANSI-stripping helper; concrete implementations (session_shell) in
sibling modules.
"""
from butterfly.tool_engine.executor.pure_context.base import strip_ansi  # noqa: F401

__all__ = ["strip_ansi"]
