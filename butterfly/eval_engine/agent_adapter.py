"""Agent adapter contract — how an arbitrary agent gets plugged in.

The eval engine knows nothing about how an agent runs. It hands the
adapter a prompt + task metadata and expects a :class:`Submission` back.
This is the only seam the reviewer needs to plug a third-party system
into our benchmark harness.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Protocol

from butterfly.eval_engine.types import EvalTask, Submission


class AgentAdapter(Protocol):
    """Anything that can answer a benchmark prompt."""

    name: str

    async def run(self, task: EvalTask) -> Submission:  # pragma: no cover - protocol
        ...


class CallableAdapter:
    """Wrap a plain (async or sync) callable as an adapter.

    The callable receives the ``EvalTask`` and must return either a
    string (taken as ``Submission.output``) or a ``Submission`` directly.
    Useful for tests, stubs, and quick external integrations.
    """

    def __init__(
        self,
        fn: Callable[[EvalTask], Any] | Callable[[EvalTask], Awaitable[Any]],
        *,
        name: str = "callable",
    ) -> None:
        self._fn = fn
        self.name = name

    async def run(self, task: EvalTask) -> Submission:
        started = time.monotonic()
        try:
            raw = self._fn(task)
            if asyncio.iscoroutine(raw):
                raw = await raw
        except Exception as exc:  # noqa: BLE001 - intentional, surfaced as error
            return Submission(
                task_id=task.task_id,
                output="",
                duration_s=time.monotonic() - started,
                error=f"{type(exc).__name__}: {exc}",
            )
        duration = time.monotonic() - started
        if isinstance(raw, Submission):
            raw.duration_s = raw.duration_s or duration
            return raw
        if isinstance(raw, str):
            return Submission(task_id=task.task_id, output=raw, duration_s=duration)
        if isinstance(raw, dict):
            return Submission(
                task_id=task.task_id,
                output=raw.get("output", ""),
                artifacts=raw.get("artifacts", {}),
                duration_s=duration,
                error=raw.get("error"),
            )
        return Submission(
            task_id=task.task_id,
            output=str(raw),
            duration_s=duration,
        )


class ButterflyAgentAdapter:
    """Drive an in-process ``butterfly.Agent`` against benchmark tasks.

    Each task runs in a fresh agent instance (``clear_history=True``) so
    the eval is hermetic. The agent's final reply is captured as the
    submission output; arbitrary structured artifacts are not extracted
    — benchmarks that need a patch / tool trace should set this up via
    their adapter-specific config.
    """

    def __init__(
        self,
        agent_factory: Callable[[], Any],
        *,
        name: str = "butterfly",
    ) -> None:
        self._factory = agent_factory
        self.name = name

    async def run(self, task: EvalTask) -> Submission:
        started = time.monotonic()
        try:
            agent = self._factory()
            result = await agent.run(task.prompt, clear_history=True)
        except Exception as exc:  # noqa: BLE001
            return Submission(
                task_id=task.task_id,
                output="",
                duration_s=time.monotonic() - started,
                error=f"{type(exc).__name__}: {exc}",
            )
        duration = time.monotonic() - started
        output = getattr(result, "output", "") or ""
        artifacts: dict[str, Any] = {}
        tool_calls = getattr(result, "tool_calls", None)
        if tool_calls:
            artifacts["tool_calls"] = [
                {"name": getattr(t, "name", "?"), "input": getattr(t, "input", {})}
                for t in tool_calls
            ]
        return Submission(
            task_id=task.task_id,
            output=output,
            artifacts=artifacts,
            duration_s=duration,
        )
