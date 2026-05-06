"""Toolhub entry — re-exports the canonical siri implementation.

The real classes live at ``butterfly/tool_engine/siri.py`` so Session can
import ``SiriRunner`` directly to register it with the
BackgroundTaskManager. This file exists solely so the conventional
``toolhub/<name>/executor.py`` discovery path in
``ToolLoader._create_executor`` finds ``SiriExecutor``.
"""
from butterfly.tool_engine.siri import (  # noqa: F401
    SiriExecutor,
    SiriRunner,
)
