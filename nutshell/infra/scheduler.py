"""Agent scheduling infrastructure — placeholder.

Future responsibilities:
- Task queue management for agent runs
- Concurrency limits across agent pool
- Retry and timeout policies
- Priority-based dispatch
- Multi-agent orchestration at the infrastructure level
"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nutshell.base.agent import BaseAgent
    from nutshell.types import AgentResult


class Scheduler:
    """Placeholder for agent task scheduling (not yet implemented).

    This class reserves the interface for the future infra layer.
    """

    def __init__(self) -> None:
        raise NotImplementedError(
            "Scheduler is a placeholder and not yet implemented. "
            "See nutshell/infra/scheduler.py for the planned interface."
        )

    async def submit(self, agent: "BaseAgent", input: str) -> "AgentResult":
        """Submit a task to the scheduler."""
        raise NotImplementedError

    async def drain(self) -> None:
        """Wait for all queued tasks to complete."""
        raise NotImplementedError
