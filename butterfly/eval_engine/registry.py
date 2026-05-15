"""Benchmark registry — thin shim over :class:`EvalLoader`.

Two coexisting discovery paths:

1. **evalhub** (production) — :class:`EvalLoader` scans
   ``evalhub/<name>/`` and instantiates the plugin's ``Adapter``.
   This is the default ``get()`` / ``list_benchmarks()`` path.

2. **in-process** (tests + external integrations) — ``register()``
   binds a factory callable to a benchmark id, taking precedence over
   the evalhub plugin with the same id. Used by tests to swap in a
   ``StubBenchmark`` without dropping files on disk.

The registry remembers no global state across processes; the loader is
the source of truth.
"""
from __future__ import annotations

from typing import Any, Callable

from butterfly.eval_engine.benchmark import Benchmark
from butterfly.eval_engine.loader import EvalLoader


BenchmarkFactory = Callable[..., Benchmark]


_loader: EvalLoader = EvalLoader()
_overrides: dict[str, BenchmarkFactory] = {}


def set_loader(loader: EvalLoader) -> None:
    """Swap the loader (used by tests pointing at a tmp evalhub)."""
    global _loader
    _loader = loader


def get_loader() -> EvalLoader:
    return _loader


def register(benchmark_id: str, factory: BenchmarkFactory) -> None:
    """Register a custom benchmark factory.

    Takes precedence over the evalhub plugin with the same id.
    Re-registering is allowed — useful for tests that swap stubs.
    """
    _overrides[benchmark_id] = factory


def unregister(benchmark_id: str) -> None:
    _overrides.pop(benchmark_id, None)


def reset_overrides() -> None:
    """Drop every in-process override; evalhub plugins remain visible."""
    _overrides.clear()


def list_benchmarks() -> list[dict]:
    """Catalog: every evalhub plugin + every in-process override.

    Each entry follows :class:`BenchmarkInfo.to_dict()` with an extra
    ``available`` boolean (currently always true; reserved for future
    capability checks).
    """
    out: dict[str, dict] = {}
    for info in _loader.list_available():
        d = info.to_dict()
        d["available"] = True
        d["source"] = "evalhub"
        out[info.id] = d
    for bid, factory in _overrides.items():
        try:
            adapter = factory()
            d = adapter.info.to_dict()
            d["available"] = True
        except Exception as exc:  # noqa: BLE001
            d = {"id": bid, "name": bid, "description": f"(init failed: {exc})",
                 "available": False}
        d["source"] = "override"
        out[bid] = d
    return list(out.values())


def get(benchmark_id: str, **kwargs: Any) -> Benchmark:
    """Resolve a benchmark id to an adapter instance.

    Lookup order: in-process overrides → evalhub plugins.
    """
    if benchmark_id in _overrides:
        return _overrides[benchmark_id](**kwargs)
    try:
        return _loader.load(benchmark_id, **kwargs)
    except KeyError as exc:
        # Re-raise with the friendlier message used by the API.
        known = sorted({b["id"] for b in list_benchmarks()})
        raise KeyError(
            f"Unknown benchmark {benchmark_id!r}. Known ids: {known}"
        ) from exc
