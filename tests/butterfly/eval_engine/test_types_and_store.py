from __future__ import annotations

from butterfly.eval_engine.store import EvalStore
from butterfly.eval_engine.types import (
    EvalRun,
    RunSummary,
    Submission,
    TaskResult,
)


def test_run_summary_from_results_empty():
    summary = RunSummary.from_results([])
    assert summary.total == 0
    assert summary.pass_rate == 0.0
    assert summary.mean_score == 0.0


def test_run_summary_buckets_results():
    results = [
        TaskResult(task_id="a", status="passed", score=1.0),
        TaskResult(task_id="b", status="passed", score=1.0),
        TaskResult(task_id="c", status="failed", score=0.0),
        TaskResult(task_id="d", status="errored"),
        TaskResult(task_id="e", status="skipped"),
    ]
    summary = RunSummary.from_results(results)
    assert summary.total == 5
    assert summary.passed == 2
    assert summary.failed == 1
    assert summary.errored == 1
    assert summary.skipped == 1
    # pass_rate is 2/3 — passed + failed graded, errored/skipped excluded.
    assert round(summary.pass_rate, 3) == round(2 / 3, 3)
    assert round(summary.mean_score, 3) == round(2 / 3, 3)


def test_store_round_trip(tmp_path):
    store = EvalStore(tmp_path / "_evals")
    run = EvalRun(
        run_id="r-1",
        benchmark="tau-bench",
        agent="echo",
        status="queued",
    )
    store.create(run)
    assert store.get("r-1") is not None

    result = TaskResult(
        task_id="t1",
        status="passed",
        score=1.0,
        submission=Submission(task_id="t1", output="hi"),
    )
    store.append_result("r-1", result)

    run.status = "completed"
    run.summary = RunSummary.from_results([result])
    store.update(run)

    fetched = store.get("r-1")
    assert fetched is not None
    assert fetched.status == "completed"
    assert fetched.summary.passed == 1
    results = list(store.read_results("r-1"))
    assert len(results) == 1
    assert results[0]["task_id"] == "t1"


def test_store_validates_run_id(tmp_path):
    store = EvalStore(tmp_path / "_evals")
    import pytest
    with pytest.raises(ValueError):
        store.run_dir("../escape")


def test_store_list_newest_first(tmp_path):
    store = EvalStore(tmp_path / "_evals")
    runs = [
        EvalRun(run_id=f"r-{i}", benchmark="tau-bench",
                agent="echo", status="completed",
                created_at=f"2026-05-{14 - i:02d}T00:00:00+00:00")
        for i in range(3)
    ]
    for r in runs:
        store.create(r)
    listed = store.list()
    assert [r.run_id for r in listed] == ["r-0", "r-1", "r-2"]
