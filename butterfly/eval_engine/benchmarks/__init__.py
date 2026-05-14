"""Built-in benchmark adapters.

These three are the canonical long-horizon agent benchmarks reported by
both Kimi K2.x and Claude Opus 4.x model cards (2025-2026):

* SWE-bench Verified — code-patching against real GitHub issues
* Terminal-bench    — multi-step shell agent tasks
* TAU-bench         — multi-turn tool-use against simulated users
"""
from butterfly.eval_engine.benchmarks.base import Benchmark
from butterfly.eval_engine.benchmarks.swe_bench import SWEBenchAdapter
from butterfly.eval_engine.benchmarks.tau_bench import TauBenchAdapter
from butterfly.eval_engine.benchmarks.terminal_bench import TerminalBenchAdapter

__all__ = [
    "Benchmark",
    "SWEBenchAdapter",
    "TerminalBenchAdapter",
    "TauBenchAdapter",
]
