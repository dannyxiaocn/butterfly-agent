"""High-level service for the eval engine.

The FastAPI router is a thin shell over this class. The CLI / scripts
can use it directly without going through HTTP. ``EvalService`` owns:

* the singleton :class:`EvalStore` (one directory per process)
* the in-flight run task table (so we can cancel)
* the adapter resolution (built-in registry by default, but the
  reviewer can pass a custom adapter via ``run_benchmark(adapter=...)``)
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from butterfly.eval_engine import registry
from butterfly.eval_engine.agent_adapter import AgentAdapter, CallableAdapter
from butterfly.eval_engine.runner import execute_run
from butterfly.eval_engine.store import EvalStore
from butterfly.eval_engine.types import EvalRun


_log = logging.getLogger(__name__)


class EvalService:
    def __init__(self, root: Path) -> None:
        self.store = EvalStore(root)
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancellers: dict[str, asyncio.Event] = {}

    # ── Catalog ──────────────────────────────────────────────────────────

    def list_benchmarks(self) -> list[dict]:
        return registry.list_benchmarks()

    # ── Runs ─────────────────────────────────────────────────────────────

    def list_runs(self) -> list[dict]:
        return [r.to_dict() for r in self.store.list()]

    def get_run(self, run_id: str) -> dict | None:
        run = self.store.get(run_id)
        return run.to_dict() if run else None

    def read_results(self, run_id: str) -> list[dict]:
        return list(self.store.read_results(run_id))

    def cancel_run(self, run_id: str) -> bool:
        ev = self._cancellers.get(run_id)
        if ev is None:
            return False
        ev.set()
        return True

    def delete_run(self, run_id: str) -> bool:
        # Block deleting a still-running run — caller should cancel first.
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            raise RuntimeError(
                f"Run {run_id!r} is still active; cancel before deleting."
            )
        return self.store.delete(run_id)

    # ── Launch ───────────────────────────────────────────────────────────

    def _mint_run_id(self, benchmark_id: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"{stamp}-{benchmark_id}-{uuid.uuid4().hex[:4]}"

    async def run_benchmark(
        self,
        benchmark_id: str,
        *,
        adapter: AgentAdapter,
        limit: int | None = None,
        parallel: int = 1,
        adapter_config: dict | None = None,
        agent_label: str | None = None,
    ) -> EvalRun:
        """Run a benchmark synchronously (awaitable). Persists the run."""
        bench = registry.get(benchmark_id, **(adapter_config or {}))
        run = EvalRun(
            run_id=self._mint_run_id(benchmark_id),
            benchmark=benchmark_id,
            agent=agent_label or getattr(adapter, "name", "unknown"),
            status="queued",
            limit=limit,
            config={
                "parallel": parallel,
                "adapter_config": dict(adapter_config or {}),
            },
        )
        self.store.create(run)
        cancel_event = asyncio.Event()
        self._cancellers[run.run_id] = cancel_event
        try:
            return await execute_run(
                run=run,
                benchmark=bench,
                adapter=adapter,
                store=self.store,
                limit=limit,
                parallel=parallel,
                cancel_event=cancel_event,
            )
        finally:
            self._cancellers.pop(run.run_id, None)

    def submit_run(
        self,
        benchmark_id: str,
        *,
        adapter: AgentAdapter,
        limit: int | None = None,
        parallel: int = 1,
        adapter_config: dict | None = None,
        agent_label: str | None = None,
    ) -> dict:
        """Fire-and-forget — schedule on the running loop, return immediately.

        The returned dict is the freshly-created :class:`EvalRun` in
        ``queued`` state; the caller polls ``GET /api/eval/runs/{id}``
        to watch it progress.
        """
        bench = registry.get(benchmark_id, **(adapter_config or {}))
        run = EvalRun(
            run_id=self._mint_run_id(benchmark_id),
            benchmark=benchmark_id,
            agent=agent_label or getattr(adapter, "name", "unknown"),
            status="queued",
            limit=limit,
            config={
                "parallel": parallel,
                "adapter_config": dict(adapter_config or {}),
            },
        )
        self.store.create(run)
        cancel_event = asyncio.Event()
        self._cancellers[run.run_id] = cancel_event

        async def _go() -> None:
            try:
                await execute_run(
                    run=run, benchmark=bench, adapter=adapter,
                    store=self.store, limit=limit, parallel=parallel,
                    cancel_event=cancel_event,
                )
            except Exception:  # noqa: BLE001
                _log.exception("eval run %s crashed", run.run_id)
                run.status = "failed"
                run.error = run.error or "runner crashed; see server log"
                self.store.update(run)
            finally:
                self._cancellers.pop(run.run_id, None)

        loop = asyncio.get_running_loop()
        task = loop.create_task(_go())
        # Drop from the in-flight table on completion so a long-lived
        # server doesn't accumulate finished tasks forever.
        task.add_done_callback(lambda _t, rid=run.run_id: self._tasks.pop(rid, None))
        self._tasks[run.run_id] = task
        return run.to_dict()


# ── Default service singleton ────────────────────────────────────────────────

_default_service: EvalService | None = None
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent / "_evals"


def get_default_service() -> EvalService:
    global _default_service
    if _default_service is None:
        _default_service = EvalService(_DEFAULT_ROOT)
    return _default_service


def set_default_service(service: EvalService) -> None:
    """Replace the singleton — used by tests to swap in a tmp-rooted store."""
    global _default_service
    _default_service = service


# ── Convenience: echo adapter (the reviewer's default when no agent is plugged) ──

def _echo_callable(task) -> str:  # type: ignore[no-untyped-def]
    return ""


def echo_adapter() -> AgentAdapter:
    """A no-op adapter — returns empty output for every task.

    Useful as a baseline for confirming the harness wiring before a real
    agent is plugged in. Every task will fail the benchmark's grader,
    which is the intended signal.
    """
    return CallableAdapter(_echo_callable, name="echo")
