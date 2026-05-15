"""TAU-bench adapter.

Upstream: https://github.com/sierra-research/tau-bench
"""
from __future__ import annotations

import json
import re
from importlib import util as _util
from typing import Any, Iterator

from butterfly.eval_engine.benchmark import Benchmark
from butterfly.eval_engine.types import (
    BenchmarkInfo,
    EvalTask,
    Submission,
    TaskResult,
)


def _upstream_available() -> bool:
    return _util.find_spec("tau_bench") is not None


_SMOKE_TASKS: list[dict] = [
    {
        "task_id": "smoke__retail-refund-001",
        "domain": "retail",
        "user_request": (
            "I'd like to refund order #A1042 — the jacket arrived "
            "damaged. My email is alice@example.com."
        ),
        "context": {
            "orders": {"A1042": {"status": "delivered", "total": 89.99}},
        },
        "expected_calls": [
            {"name": "lookup_order", "arguments": {"order_id": "A1042"}},
            {"name": "issue_refund", "arguments": {"order_id": "A1042"}},
        ],
    },
    {
        "task_id": "smoke__retail-cancel-002",
        "domain": "retail",
        "user_request": (
            "Please cancel order #B7781 if it hasn't shipped yet."
        ),
        "context": {
            "orders": {"B7781": {"status": "processing", "total": 42.0}},
        },
        "expected_calls": [
            {"name": "lookup_order", "arguments": {"order_id": "B7781"}},
            {"name": "cancel_order", "arguments": {"order_id": "B7781"}},
        ],
    },
    {
        "task_id": "smoke__airline-rebook-003",
        "domain": "airline",
        "user_request": (
            "Rebook reservation R-9988 from tomorrow's flight to the "
            "next available one with the same airline."
        ),
        "context": {
            "reservations": {"R-9988": {"status": "confirmed", "flight": "UA123"}},
        },
        "expected_calls": [
            {"name": "lookup_reservation", "arguments": {"reservation_id": "R-9988"}},
            {"name": "rebook_reservation", "arguments": {"reservation_id": "R-9988"}},
        ],
    },
]


_LINE_CALL_RE = re.compile(r"(\w+)\s*\((.*)\)\s*$")
_ARG_RE = re.compile(r"(\w+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^,)]+)")


def _parse_trace(output: str) -> list[dict]:
    text = (output or "").strip()
    if not text:
        return []
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    try:
        data = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        data = None
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict) and "name" in c]
    if isinstance(data, dict) and isinstance(data.get("tool_calls"), list):
        return [c for c in data["tool_calls"] if isinstance(c, dict) and "name" in c]
    calls: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip().rstrip(",")
        if not line:
            continue
        m = _LINE_CALL_RE.match(line)
        if not m:
            continue
        name, args_blob = m.group(1), m.group(2)
        args: dict[str, Any] = {}
        for am in _ARG_RE.finditer(args_blob):
            k, v = am.group(1), am.group(2).strip().rstrip(")").strip("\"'")
            args[k] = v
        calls.append({"name": name, "arguments": args})
    return calls


def _arg_matches(expected: dict, got: dict) -> bool:
    for k, v in expected.items():
        if str(got.get(k, "")) != str(v):
            return False
    return True


class Adapter(Benchmark):
    def __init__(
        self,
        info: BenchmarkInfo,
        *,
        mode: str = "auto",
        domain: str | None = None,
    ) -> None:
        if mode not in ("auto", "smoke", "upstream"):
            raise ValueError(f"Unknown mode: {mode!r}")
        if mode == "auto":
            mode = "upstream" if _upstream_available() else "smoke"
        self.info = info
        self.mode = mode
        self.domain = domain

    def iter_tasks(self, *, limit: int | None = None) -> Iterator[EvalTask]:
        if self.mode == "upstream":
            yield from self._iter_upstream(limit=limit)
            return
        pool = _SMOKE_TASKS
        if self.domain:
            pool = [t for t in pool if t["domain"] == self.domain]
        for spec in pool[: limit or len(pool)]:
            yield EvalTask(
                task_id=spec["task_id"],
                benchmark=self.info.id,
                prompt=(
                    "You are a TAU-bench customer-service agent. The "
                    f"user (domain: {spec['domain']}) says:\n\n"
                    f"    {spec['user_request']}\n\n"
                    "Context (DB snapshot):\n"
                    f"{json.dumps(spec['context'], indent=2)}\n\n"
                    "Reply with ONLY a JSON list of tool calls, each "
                    "shaped as {\"name\": ..., \"arguments\": {...}}."
                ),
                metadata={
                    "task_id": spec["task_id"],
                    "domain": spec["domain"],
                    "expected_calls": list(spec["expected_calls"]),
                    # Used by the CLI's mock-passing adapter as a
                    # universal "pass" signal (PR #72 review item 1).
                    "expected_output": json.dumps(spec["expected_calls"]),
                    "mode": "smoke",
                },
            )

    def _iter_upstream(self, *, limit: int | None) -> Iterator[EvalTask]:
        try:
            from tau_bench.envs.retail.tasks import TASKS as retail_tasks  # type: ignore[import-not-found]
            from tau_bench.envs.airline.tasks import TASKS as airline_tasks  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"tau_bench upstream import failed: {exc!r}. Reinstall "
                "with `pip install tau-bench`."
            )
        pool = []
        for source, domain in ((retail_tasks, "retail"), (airline_tasks, "airline")):
            if self.domain and self.domain != domain:
                continue
            for spec in source:
                pool.append((domain, spec))
        for i, (domain, spec) in enumerate(pool):
            if limit is not None and i >= limit:
                break
            yield EvalTask(
                task_id=f"{domain}__{getattr(spec, 'task_id', i)}",
                benchmark=self.info.id,
                prompt=getattr(spec, "instruction", ""),
                metadata={"domain": domain, "mode": "upstream"},
            )

    def grade(self, task: EvalTask, submission: Submission) -> TaskResult:
        if submission.error:
            return TaskResult(
                task_id=task.task_id,
                status="errored",
                details={"error": submission.error},
                submission=submission,
            )
        if task.metadata.get("mode") == "upstream":
            return TaskResult(
                task_id=task.task_id,
                status="skipped",
                details={
                    "note": (
                        "upstream grading is deferred to the tau-bench "
                        "harness; re-grade with `python -m tau_bench.run`."
                    ),
                    "trace": submission.output,
                },
                submission=submission,
            )
        return self._grade_smoke(task, submission)

    def _grade_smoke(self, task: EvalTask, submission: Submission) -> TaskResult:
        expected = task.metadata.get("expected_calls", [])
        got = _parse_trace(submission.output)
        cursor = 0
        misses: list[dict] = []
        for exp in expected:
            found = False
            for j in range(cursor, len(got)):
                cand = got[j]
                if (
                    cand.get("name") == exp["name"]
                    and _arg_matches(exp.get("arguments", {}), cand.get("arguments", {}))
                ):
                    cursor = j + 1
                    found = True
                    break
            if not found:
                misses.append(exp)
        passed = not misses
        return TaskResult(
            task_id=task.task_id,
            status="passed" if passed else "failed",
            score=1.0 if passed else max(0.0, 1 - len(misses) / max(1, len(expected))),
            details={
                "expected_calls": expected,
                "parsed_trace": got,
                "missing_calls": misses,
            },
            submission=submission,
        )
