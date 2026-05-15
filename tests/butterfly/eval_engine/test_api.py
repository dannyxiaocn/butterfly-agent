"""FastAPI router tests using fastapi.testclient."""
from __future__ import annotations

import asyncio
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from butterfly.eval_engine import registry
from butterfly.eval_engine.agent_adapter import CallableAdapter
from butterfly.eval_engine.api import (
    create_router,
    register_adapter,
    unregister_adapter,
)
from butterfly.eval_engine.benchmark import Benchmark
from butterfly.eval_engine.service import EvalService
from butterfly.eval_engine.types import BenchmarkInfo, EvalTask, TaskResult


class StubBench(Benchmark):
    info = BenchmarkInfo(
        id="stub", name="stub", description="", homepage="",
        task_type="stub", metric="exact_match", default_limit=2,
    )

    def iter_tasks(self, *, limit=None) -> Iterator[EvalTask]:
        for i in range(min(2, limit or 2)):
            yield EvalTask(
                task_id=f"t{i}", benchmark="stub",
                prompt=str(i), metadata={"expected": str(i)},
            )

    def grade(self, task, submission):
        ok = submission.output.strip() == task.metadata["expected"]
        return TaskResult(
            task_id=task.task_id,
            status="passed" if ok else "failed",
            score=1.0 if ok else 0.0,
            submission=submission,
        )


@pytest.fixture
def client(tmp_path):
    registry.register("stub", StubBench)
    register_adapter(
        "perfect",
        CallableAdapter(lambda t: t.metadata["expected"], name="perfect"),
    )
    svc = EvalService(tmp_path / "_evals")
    app = FastAPI()
    app.include_router(create_router(svc))
    with TestClient(app) as c:
        yield c
    unregister_adapter("perfect")
    registry.reset_overrides()


def test_list_benchmarks(client):
    r = client.get("/api/eval/benchmarks")
    assert r.status_code == 200
    ids = {b["id"] for b in r.json()["benchmarks"]}
    assert {"swe-bench-verified", "terminal-bench", "tau-bench", "stub"} <= ids


def test_list_adapters(client):
    r = client.get("/api/eval/adapters")
    assert r.status_code == 200
    assert "perfect" in r.json()["adapters"]
    assert "echo" in r.json()["adapters"]


def test_post_run_sync_returns_completed(client):
    r = client.post(
        "/api/eval/runs",
        json={"benchmark": "stub", "adapter": "perfect", "sync": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["summary"]["passed"] == 2


def test_post_run_unknown_benchmark_returns_404(client):
    r = client.post(
        "/api/eval/runs",
        json={"benchmark": "nope", "adapter": "perfect", "sync": True},
    )
    assert r.status_code == 404


def test_post_run_unknown_adapter_returns_400(client):
    r = client.post(
        "/api/eval/runs",
        json={"benchmark": "stub", "adapter": "ghost", "sync": True},
    )
    assert r.status_code == 400


def test_post_run_missing_benchmark_returns_400(client):
    r = client.post("/api/eval/runs", json={})
    assert r.status_code == 400


def test_get_run_and_results(client):
    r = client.post(
        "/api/eval/runs",
        json={"benchmark": "stub", "adapter": "perfect", "sync": True},
    )
    run_id = r.json()["run_id"]
    r2 = client.get(f"/api/eval/runs/{run_id}")
    assert r2.status_code == 200
    assert r2.json()["status"] == "completed"
    r3 = client.get(f"/api/eval/runs/{run_id}/results")
    assert r3.status_code == 200
    assert len(r3.json()["results"]) == 2


def test_get_unknown_run_returns_404(client):
    assert client.get("/api/eval/runs/nope").status_code == 404


def test_delete_run(client):
    r = client.post(
        "/api/eval/runs",
        json={"benchmark": "stub", "adapter": "perfect", "sync": True},
    )
    run_id = r.json()["run_id"]
    d = client.delete(f"/api/eval/runs/{run_id}")
    assert d.status_code == 200
    assert d.json()["deleted"] is True
    assert client.get(f"/api/eval/runs/{run_id}").status_code == 404


def test_cancel_run_for_unknown_run_returns_404(client):
    assert client.post("/api/eval/runs/nope/cancel").status_code == 404
