"""Dataclasses for the eval engine — the wire shape for runs/results.

These types are the contract between the runner, the benchmark adapters,
the persistence layer, and the FastAPI surface. Keep them JSON-round-trippable
so the API can serialise/deserialise without a schema library.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


RunStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
TaskStatus = Literal["pending", "running", "passed", "failed", "errored", "skipped"]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class BenchmarkInfo:
    """Static descriptor for a benchmark adapter (the catalog entry)."""
    id: str
    name: str
    description: str
    homepage: str
    task_type: str
    metric: str
    requires_docker: bool = False
    requires_network: bool = True
    default_limit: int = 10

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvalTask:
    """One unit of work — a single problem instance fed to the agent."""
    task_id: str
    benchmark: str
    prompt: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Submission:
    """The agent's output for a single task.

    ``output`` is the raw textual reply; ``artifacts`` collects structured
    outputs (a patch, a tool-call trace, a final answer) that the
    benchmark needs to grade. Adapters are free to put anything in
    ``artifacts`` — the engine only round-trips it as JSON.
    """
    task_id: str
    output: str
    artifacts: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TaskResult:
    task_id: str
    status: TaskStatus
    score: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)
    submission: Submission | None = None
    started_at: str = field(default_factory=_utcnow_iso)
    ended_at: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.submission is not None:
            d["submission"] = self.submission.to_dict()
        return d


@dataclass
class RunSummary:
    """Aggregate metrics across every TaskResult in a run."""
    total: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0
    skipped: int = 0
    pass_rate: float = 0.0
    mean_score: float = 0.0

    @classmethod
    def from_results(cls, results: list[TaskResult]) -> "RunSummary":
        if not results:
            return cls()
        passed = sum(1 for r in results if r.status == "passed")
        failed = sum(1 for r in results if r.status == "failed")
        errored = sum(1 for r in results if r.status == "errored")
        skipped = sum(1 for r in results if r.status == "skipped")
        graded = [r for r in results if r.status in ("passed", "failed")]
        mean = sum(r.score for r in graded) / len(graded) if graded else 0.0
        rate = passed / len(graded) if graded else 0.0
        return cls(
            total=len(results),
            passed=passed,
            failed=failed,
            errored=errored,
            skipped=skipped,
            pass_rate=rate,
            mean_score=mean,
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvalRun:
    """A single execution of one benchmark against one agent."""
    run_id: str
    benchmark: str
    agent: str
    status: RunStatus
    created_at: str = field(default_factory=_utcnow_iso)
    started_at: str | None = None
    ended_at: str | None = None
    limit: int | None = None
    config: dict[str, Any] = field(default_factory=dict)
    summary: RunSummary = field(default_factory=RunSummary)
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["summary"] = self.summary.to_dict()
        return d
