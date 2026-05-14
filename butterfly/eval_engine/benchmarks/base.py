"""Benchmark adapter interface.

A benchmark adapter knows three things:

1. how to enumerate its task instances (``iter_tasks``)
2. what static info to expose (``info``)
3. how to grade a submission (``grade``)

The runner is benchmark-agnostic; everything benchmark-specific lives
behind these three methods. Real benchmarks shell out to upstream
harnesses (Docker, swebench's evaluator, terminal-bench's runner); the
adapter is the seam where that machinery hides.
"""
from __future__ import annotations

import abc
from typing import Iterator

from butterfly.eval_engine.types import (
    BenchmarkInfo,
    EvalTask,
    Submission,
    TaskResult,
)


class Benchmark(abc.ABC):
    """Abstract benchmark adapter."""

    info: BenchmarkInfo

    @abc.abstractmethod
    def iter_tasks(self, *, limit: int | None = None) -> Iterator[EvalTask]:
        """Yield task instances. Honour ``limit`` for quick runs."""

    @abc.abstractmethod
    def grade(self, task: EvalTask, submission: Submission) -> TaskResult:
        """Score one submission against the task's expected outcome."""

    @property
    def id(self) -> str:
        return self.info.id
