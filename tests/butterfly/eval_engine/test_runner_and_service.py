"""End-to-end tests for the runner + service.

Uses a stub benchmark + a CallableAdapter so we can drive a full run
without any external dependencies. The real adapters are exercised by
``test_benchmarks.py``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Iterator

import pytest

from butterfly.eval_engine import registry
from butterfly.eval_engine.agent_adapter import CallableAdapter
from butterfly.eval_engine.benchmarks.base import Benchmark
from butterfly.eval_engine.service import EvalService
from butterfly.eval_engine.types import (
    BenchmarkInfo,
    EvalTask,
    Submission,
    TaskResult,
)


# ── Stub benchmark for runner-level tests ────────────────────────────────────

class StubBenchmark(Benchmark):
    info = BenchmarkInfo(
        id="stub",
        name="stub",
        description="test-only",
        homepage="",
        task_type="stub",
        metric="exact_match",
        default_limit=3,
    )

    def __init__(self, *, n: int = 3) -> None:
        self.n = n

    def iter_tasks(self, *, limit: int | None = None) -> Iterator[EvalTask]:
        for i in range(min(self.n, limit or self.n)):
            yield EvalTask(
                task_id=f"t{i}",
                benchmark="stub",
                prompt=f"echo {i}",
                metadata={"expected": str(i)},
            )

    def grade(self, task, submission):
        if submission.error:
            return TaskResult(task_id=task.task_id, status="errored",
                              details={"error": submission.error},
                              submission=submission)
        ok = submission.output.strip() == task.metadata["expected"]
        return TaskResult(
            task_id=task.task_id,
            status="passed" if ok else "failed",
            score=1.0 if ok else 0.0,
            submission=submission,
        )


@pytest.fixture(autouse=True)
def _register_stub():
    registry.register("stub", StubBenchmark)
    yield
    registry.reset_to_builtins()


# ── run_benchmark (synchronous await) ────────────────────────────────────────

async def test_run_benchmark_all_pass(tmp_path):
    svc = EvalService(tmp_path / "_evals")
    adapter = CallableAdapter(lambda t: t.metadata["expected"], name="perfect")
    run = await svc.run_benchmark("stub", adapter=adapter)
    assert run.status == "completed"
    assert run.summary.passed == 3
    assert run.summary.failed == 0
    assert run.summary.pass_rate == 1.0


async def test_run_benchmark_records_failures(tmp_path):
    svc = EvalService(tmp_path / "_evals")
    adapter = CallableAdapter(lambda t: "wrong", name="dumb")
    run = await svc.run_benchmark("stub", adapter=adapter)
    assert run.status == "completed"
    assert run.summary.failed == 3
    assert run.summary.passed == 0


async def test_run_benchmark_captures_adapter_exception(tmp_path):
    svc = EvalService(tmp_path / "_evals")
    def boom(_task):
        raise RuntimeError("kaboom")
    adapter = CallableAdapter(boom, name="boom")
    run = await svc.run_benchmark("stub", adapter=adapter)
    assert run.status == "completed"
    assert run.summary.errored == 3


async def test_run_benchmark_respects_limit(tmp_path):
    svc = EvalService(tmp_path / "_evals")
    adapter = CallableAdapter(lambda t: t.metadata["expected"], name="ok")
    run = await svc.run_benchmark("stub", adapter=adapter, limit=1)
    assert run.summary.total == 1
    results = svc.read_results(run.run_id)
    assert len(results) == 1


async def test_run_benchmark_parallel(tmp_path):
    svc = EvalService(tmp_path / "_evals")
    inflight = 0
    max_inflight = 0
    lock = asyncio.Lock()

    async def slow_adapter(task):
        nonlocal inflight, max_inflight
        async with lock:
            inflight += 1
            max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.05)
        async with lock:
            inflight -= 1
        return task.metadata["expected"]

    adapter = CallableAdapter(slow_adapter, name="slow")
    run = await svc.run_benchmark("stub", adapter=adapter, parallel=3)
    assert run.summary.passed == 3
    assert max_inflight >= 2


async def test_submit_run_returns_queued_state(tmp_path):
    svc = EvalService(tmp_path / "_evals")
    adapter = CallableAdapter(lambda t: t.metadata["expected"], name="ok")
    payload = svc.submit_run("stub", adapter=adapter)
    assert payload["status"] == "queued"
    # Drain the background task so the test doesn't leak it.
    task = svc._tasks[payload["run_id"]]
    await task
    final = svc.get_run(payload["run_id"])
    assert final["status"] == "completed"


# ── Catalog ──────────────────────────────────────────────────────────────────

def test_list_benchmarks_includes_builtins(tmp_path):
    svc = EvalService(tmp_path / "_evals")
    catalog = svc.list_benchmarks()
    ids = {b["id"] for b in catalog}
    assert {"swe-bench-verified", "terminal-bench", "tau-bench"} <= ids


def test_unknown_benchmark_raises(tmp_path):
    with pytest.raises(KeyError):
        registry.get("does-not-exist")


# ── Run lifecycle (delete / cancel guard) ────────────────────────────────────

def test_delete_unknown_run_returns_false(tmp_path):
    svc = EvalService(tmp_path / "_evals")
    assert svc.delete_run("nope") is False


async def test_delete_running_run_is_blocked(tmp_path):
    svc = EvalService(tmp_path / "_evals")

    async def slow_adapter(task):
        await asyncio.sleep(0.5)
        return task.metadata["expected"]

    adapter = CallableAdapter(slow_adapter, name="slow")
    payload = svc.submit_run("stub", adapter=adapter)
    run_id = payload["run_id"]
    try:
        with pytest.raises(RuntimeError):
            svc.delete_run(run_id)
    finally:
        await svc._tasks[run_id]
