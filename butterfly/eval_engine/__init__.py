"""Butterfly EvalEngine — a benchmark harness for long-horizon agents.

Ships three benchmark adapters that mirror the reference set reported
by Claude Opus 4.x and Kimi K2.x model cards (2025-2026):

* **SWE-bench Verified** — code-patching against real GitHub issues
* **Terminal-bench**     — multi-step shell agent tasks
* **TAU-bench**          — multi-turn tool-use against simulated users

Quick start (Python)::

    from butterfly.eval_engine import EvalService, CallableAdapter

    svc = EvalService(Path("./_evals"))
    adapter = CallableAdapter(lambda task: "...agent reply...", name="my-agent")
    run = await svc.run_benchmark("tau-bench", adapter=adapter, limit=3)
    print(run.summary)

Or via HTTP — the :mod:`butterfly.eval_engine.api` router is mounted
under ``/api/eval/...`` by the web app and is the interface the
reviewer hits from a CI workflow.
"""
from butterfly.eval_engine.agent_adapter import (
    AgentAdapter,
    ButterflyAgentAdapter,
    CallableAdapter,
)
from butterfly.eval_engine.benchmarks import (
    Benchmark,
    SWEBenchAdapter,
    TauBenchAdapter,
    TerminalBenchAdapter,
)
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
    "EvalRun",
    "EvalService",
    "EvalStore",
    "EvalTask",
    "RunSummary",
    "Submission",
    "SWEBenchAdapter",
    "TaskResult",
    "TauBenchAdapter",
    "TerminalBenchAdapter",
    "echo_adapter",
    "execute_run",
    "get_default_service",
    "set_default_service",
]
