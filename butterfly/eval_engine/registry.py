"""Benchmark registry.

Centralises the {id: factory} mapping. The runner and the API both ask
the registry for an adapter instance — neither imports a concrete
benchmark module directly.
"""
from __future__ import annotations

from typing import Any, Callable

from butterfly.eval_engine.benchmarks import (
    Benchmark,
    SWEBenchAdapter,
    TauBenchAdapter,
    TerminalBenchAdapter,
)


BenchmarkFactory = Callable[..., Benchmark]


_BUILTINS: dict[str, BenchmarkFactory] = {
    "swe-bench-verified": SWEBenchAdapter,
    "terminal-bench": TerminalBenchAdapter,
    "tau-bench": TauBenchAdapter,
}


_registry: dict[str, BenchmarkFactory] = dict(_BUILTINS)


def register(benchmark_id: str, factory: BenchmarkFactory) -> None:
    """Register a custom benchmark factory.

    Re-registering an id is allowed — useful for tests that need to
    swap in a stub.
    """
    _registry[benchmark_id] = factory


def unregister(benchmark_id: str) -> None:
    _registry.pop(benchmark_id, None)


def reset_to_builtins() -> None:
    """Drop every registered benchmark except the three built-ins."""
    _registry.clear()
    _registry.update(_BUILTINS)


def list_benchmarks() -> list[dict]:
    """Return the static info for every registered benchmark.

    Each entry follows :class:`BenchmarkInfo.to_dict()` — id, name,
    description, homepage, metric, etc.
    """
    out: list[dict] = []
    for bid, factory in _registry.items():
        try:
            adapter = factory()
        except Exception as exc:  # noqa: BLE001 - keep the catalog usable
            out.append({
                "id": bid,
                "name": bid,
                "description": f"(adapter init failed: {exc})",
                "available": False,
            })
            continue
        info = adapter.info.to_dict()
        info["available"] = True
        out.append(info)
    return out


def get(benchmark_id: str, **kwargs: Any) -> Benchmark:
    """Resolve a benchmark id to an adapter instance.

    Extra kwargs flow into the factory — adapters accept ``mode=`` so
    the reviewer can pin ``mode="smoke"`` from the API.
    """
    if benchmark_id not in _registry:
        raise KeyError(
            f"Unknown benchmark {benchmark_id!r}. Known ids: "
            f"{sorted(_registry)}"
        )
    return _registry[benchmark_id](**kwargs)
