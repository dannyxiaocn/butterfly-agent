"""Butterfly EvalEngine — a benchmark harness for long-horizon agents.

Benchmarks ship as **evalhub plugins** (mirror of ``toolhub`` /
``skillhub``):

    evalhub/<name>/
      eval.json       # static descriptor (BenchmarkInfo)
      adapter.py      # defines `class Adapter(Benchmark)`

An ``evals.md`` file (or a per-suite manifest) selects which plugins
are enabled — one name per line, just like ``tools.md`` / ``skills.md``.

The three built-in plugins mirror the long-horizon set reported by
Claude Opus 4.x and Kimi K2.x in 2025-2026:

* **swe-bench-verified** — code patches against real GitHub issues
* **terminal-bench**     — multi-step shell agent tasks
* **tau-bench**          — multi-turn tool-use against simulated users

Quick start (Python)::

    from butterfly.eval_engine import EvalService, CallableAdapter

    svc = EvalService(Path("./_evals"))
    adapter = CallableAdapter(lambda task: "...agent reply...", name="my-agent")
    run = await svc.run_benchmark("tau-bench", adapter=adapter, limit=3)
    print(run.summary)

CLI::

    butterfly eval list                     # show available + enabled plugins
    butterfly eval run --enable tau-bench,terminal-bench --limit 3
"""
from butterfly.eval_engine.agent_adapter import (
    AgentAdapter,
    ButterflyAgentAdapter,
    CallableAdapter,
)
from butterfly.eval_engine.benchmark import Benchmark
from butterfly.eval_engine.loader import EvalLoader
from butterfly.eval_engine.runner import execute_run
from butterfly.eval_engine.service import (
    EvalService,
    echo_adapter,
    get_default_service,
    set_default_service,
)
from butterfly.eval_engine.store import EvalStore
from butterfly.eval_engine.types import (
    BenchmarkInfo,
    EvalRun,
    EvalTask,
    RunSummary,
    Submission,
    TaskResult,
)

__all__ = [
    "AgentAdapter",
    "ButterflyAgentAdapter",
    "CallableAdapter",
    "Benchmark",
    "BenchmarkInfo",
    "EvalLoader",
    "EvalRun",
    "EvalService",
    "EvalStore",
    "EvalTask",
    "RunSummary",
    "Submission",
    "TaskResult",
    "echo_adapter",
    "execute_run",
    "get_default_service",
    "set_default_service",
]
