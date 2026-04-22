"""Toolhub entry — re-exports the canonical sub_agent implementation.

The real classes live at ``butterfly/tool_engine/sub_agent.py`` so they can be
imported normally (Session needs ``SubAgentRunner`` to register with the
BackgroundTaskManager). This file exists solely so the conventional
``toolhub/<name>/executor.py`` discovery path in ``ToolLoader._create_executor``
finds ``SubAgentTool``.
"""
from butterfly.tool_engine.sub_agent import (  # noqa: F401
    SubAgentRunner,
    SubAgentTool,
)
