"""The eval runner — wires a benchmark to an adapter and records results.

The runner is intentionally simple: it iterates a benchmark's tasks,
calls ``adapter.run(task)``, hands the submission to ``benchmark.grade``,
and persists the result. Concurrency is bounded by ``parallel``; the
runner is asyncio-native so the underlying adapter can be I/O-bound
without blocking the loop.

Cancellation: the runner checks ``cancel_event`` between task starts
and between gather-batches. Already-running adapter calls are NOT
forcibly killed — we trust the adapter to be cooperatively cancellable
(asyncio.CancelledError will propagate through ``await adapter.run``).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Iterable

from butterfly.eval_engine.agent_adapter import AgentAdapter
from butterfly.eval_engine.benchmark import Benchmark
from butterfly.eval_engine.store import EvalStore
from butterfly.eval_engine.types import (
    EvalRun,
    EvalTask,
    RunSummary,
    TaskResult,
)


_log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def execute_run(
    *,
    run: EvalRun,
    benchmark: Benchmark,
    adapter: AgentAdapter,
    store: EvalStore,
    limit: int | None = None,
    parallel: int = 1,
    cancel_event: asyncio.Event | None = None,
) -> EvalRun:
    """Drive a benchmark against an adapter; persist as we go.

    Returns the final :class:`EvalRun` (also persisted to ``store``).
    """
    if parallel < 1:
        raise ValueError("parallel must be >= 1")
    run.status = "running"
    run.started_at = _now()
    store.update(run)

    sem = asyncio.Semaphore(parallel)
    results: list[TaskResult] = []
    cancel_event = cancel_event or asyncio.Event()

    async def _one(task: EvalTask) -> TaskResult:
        async with sem:
            if cancel_event.is_set():
                return TaskResult(
                    task_id=task.task_id, status="skipped",
                    details={"reason": "run cancelled"},
                )
            try:
                submission = await adapter.run(task)
                result = benchmark.grade(task, submission)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log.exception("task %s raised", task.task_id)
                result = TaskResult(
                    task_id=task.task_id,
                    status="errored",
                    details={"error": f"{type(exc).__name__}: {exc}"},
                )
            result.ended_at = _now()
            store.append_result(run.run_id, result)
            return result

    try:
        tasks: Iterable[EvalTask] = list(benchmark.iter_tasks(limit=limit))
    except Exception as exc:  # noqa: BLE001 — adapter discovery failed
        run.status = "failed"
        run.ended_at = _now()
        run.error = f"task discovery failed: {type(exc).__name__}: {exc}"
        store.update(run)
        return run

    try:
        coros = [_one(t) for t in tasks]
        for coro in asyncio.as_completed(coros):
            result = await coro
            results.append(result)
            # Update aggregate on the fly so the API can show live progress.
            run.summary = RunSummary.from_results(results)
            store.update(run)
            if cancel_event.is_set():
                # Don't break — let in-flight tasks finish so we get a
                # consistent results.jsonl. as_completed will deliver
                # the rest as cancelled/skipped via the semaphore check.
                continue
    except asyncio.CancelledError:
        run.status = "cancelled"
        run.ended_at = _now()
        store.update(run)
        raise

    run.status = "cancelled" if cancel_event.is_set() else "completed"
    run.ended_at = _now()
    run.summary = RunSummary.from_results(results)
    store.update(run)
    return run
