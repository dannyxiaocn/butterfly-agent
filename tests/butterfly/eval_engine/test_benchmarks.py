"""Per-benchmark smoke tests.

Each adapter must:
1. enumerate at least one smoke task
2. grade a hand-crafted "good" submission as passed
3. grade an obviously-bad submission as failed
"""
from __future__ import annotations

import json

import pytest

from butterfly.eval_engine.benchmarks import (
    SWEBenchAdapter,
    TauBenchAdapter,
    TerminalBenchAdapter,
)
from butterfly.eval_engine.types import Submission


# ── SWE-bench ────────────────────────────────────────────────────────────────

def test_swe_smoke_iter_tasks():
    bench = SWEBenchAdapter(mode="smoke")
    tasks = list(bench.iter_tasks(limit=2))
    assert len(tasks) == 2
    assert all(t.metadata["mode"] == "smoke" for t in tasks)
    assert all("expected_substrings" in t.metadata for t in tasks)


def test_swe_smoke_grades_correct_patch():
    bench = SWEBenchAdapter(mode="smoke")
    task = next(bench.iter_tasks(limit=1))
    # Build a unified-diff that mentions the expected line and the
    # expected file path.
    target_file = task.metadata["expected_changed_files"][0]
    expected_line = task.metadata["expected_substrings"][0]
    patch = (
        f"diff --git a/{target_file} b/{target_file}\n"
        f"--- a/{target_file}\n"
        f"+++ b/{target_file}\n"
        "@@\n"
        f"+    {expected_line}\n"
    )
    result = bench.grade(task, Submission(task_id=task.task_id, output=patch))
    assert result.status == "passed", result.details


def test_swe_smoke_rejects_bare_text():
    bench = SWEBenchAdapter(mode="smoke")
    task = next(bench.iter_tasks(limit=1))
    result = bench.grade(task, Submission(task_id=task.task_id,
                                          output="here is what I'd do..."))
    assert result.status == "failed"
    assert not result.details["looks_like_patch"]


def test_swe_grade_propagates_submission_error():
    bench = SWEBenchAdapter(mode="smoke")
    task = next(bench.iter_tasks(limit=1))
    result = bench.grade(task, Submission(task_id=task.task_id,
                                          output="", error="boom"))
    assert result.status == "errored"


# ── Terminal-bench ───────────────────────────────────────────────────────────

def test_tbench_smoke_iter_tasks():
    bench = TerminalBenchAdapter(mode="smoke")
    tasks = list(bench.iter_tasks(limit=3))
    assert len(tasks) == 3
    assert {t.task_id for t in tasks} == {
        "smoke__write-hello", "smoke__count-files", "smoke__tar-archive",
    }


def test_tbench_smoke_correct_command_passes():
    bench = TerminalBenchAdapter(mode="smoke")
    task = next(t for t in bench.iter_tasks() if t.task_id == "smoke__write-hello")
    submission = Submission(
        task_id=task.task_id,
        output="printf 'hello world' > hello.txt",
    )
    result = bench.grade(task, submission)
    assert result.status == "passed", result.details


def test_tbench_smoke_wrong_command_fails():
    bench = TerminalBenchAdapter(mode="smoke")
    task = next(t for t in bench.iter_tasks() if t.task_id == "smoke__write-hello")
    submission = Submission(task_id=task.task_id, output="ls")
    result = bench.grade(task, submission)
    assert result.status == "failed"


def test_tbench_smoke_rejects_unsafe_command():
    bench = TerminalBenchAdapter(mode="smoke")
    task = next(t for t in bench.iter_tasks() if t.task_id == "smoke__write-hello")
    submission = Submission(task_id=task.task_id, output="sudo rm -rf /")
    result = bench.grade(task, submission)
    assert result.status == "errored"
    assert "refused" in result.details["reason"]


def test_tbench_count_files_uses_setup():
    bench = TerminalBenchAdapter(mode="smoke")
    task = next(t for t in bench.iter_tasks() if t.task_id == "smoke__count-files")
    # Three files set up; correct answer is 3.
    submission = Submission(task_id=task.task_id, output="echo 3 > count.txt")
    result = bench.grade(task, submission)
    assert result.status == "passed", result.details


# ── TAU-bench ────────────────────────────────────────────────────────────────

def test_tau_smoke_iter_tasks():
    bench = TauBenchAdapter(mode="smoke")
    tasks = list(bench.iter_tasks())
    assert len(tasks) == 3
    assert {t.metadata["domain"] for t in tasks} == {"retail", "airline"}


def test_tau_smoke_domain_filter():
    bench = TauBenchAdapter(mode="smoke", domain="retail")
    tasks = list(bench.iter_tasks())
    assert tasks
    assert all(t.metadata["domain"] == "retail" for t in tasks)


def test_tau_smoke_json_trace_passes():
    bench = TauBenchAdapter(mode="smoke")
    task = next(t for t in bench.iter_tasks() if t.task_id.startswith("smoke__retail-refund"))
    expected = task.metadata["expected_calls"]
    trace = json.dumps(expected)
    result = bench.grade(task, Submission(task_id=task.task_id, output=trace))
    assert result.status == "passed", result.details


def test_tau_smoke_line_trace_passes():
    bench = TauBenchAdapter(mode="smoke")
    task = next(t for t in bench.iter_tasks() if t.task_id.startswith("smoke__retail-cancel"))
    expected = task.metadata["expected_calls"]
    quote = '"'
    lines = []
    for c in expected:
        args_str = ", ".join(
            f"{k}={quote}{v}{quote}" for k, v in c["arguments"].items()
        )
        lines.append(f"{c['name']}({args_str})")
    result = bench.grade(task, Submission(task_id=task.task_id, output="\n".join(lines)))
    assert result.status == "passed", result.details


def test_tau_smoke_unquoted_args_strip_trailing_paren():
    """Regression: _ARG_RE used to greedily capture the closing `)` from
    line-form tool calls with unquoted values, so ``f(x=1)`` parsed
    as ``x="1)"``."""
    from butterfly.eval_engine.benchmarks.tau_bench import _parse_trace
    parsed = _parse_trace("lookup_order(order_id=A1042)")
    assert parsed == [{"name": "lookup_order", "arguments": {"order_id": "A1042"}}]


def test_tau_smoke_missing_call_fails():
    bench = TauBenchAdapter(mode="smoke")
    task = next(iter(bench.iter_tasks()))
    # Output a call with the right name but wrong order / missing follow-up.
    output = json.dumps([{"name": "noop", "arguments": {}}])
    result = bench.grade(task, Submission(task_id=task.task_id, output=output))
    assert result.status == "failed"
    assert result.details["missing_calls"]


def test_unknown_mode_raises():
    for cls in (SWEBenchAdapter, TerminalBenchAdapter, TauBenchAdapter):
        with pytest.raises(ValueError):
            cls(mode="bogus")
