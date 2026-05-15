"""FastAPI router for the eval engine — `/api/eval/...`.

Mounted by ``ui/web/app.py::create_app``. The reviewer (or any external
caller in a CI workflow) hits these endpoints to:

* discover the catalog of benchmarks
* launch a run against a registered adapter
* poll for progress / pull final results
* cancel an in-flight run

The router never touches the store/registry directly; it goes through
:class:`butterfly.eval_engine.service.EvalService` so the CLI and the
HTTP layer share one code path. The default adapter is the "echo"
placeholder; the reviewer wires a real agent by registering a named
adapter via :func:`register_adapter` (typically in a startup hook).
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

from butterfly.eval_engine.agent_adapter import AgentAdapter, CallableAdapter
from butterfly.eval_engine.service import EvalService, echo_adapter, get_default_service


# ── Adapter registry (name → factory) ─────────────────────────────────────────
#
# The HTTP layer can only refer to adapters by string name (you can't
# pickle a Python callable into JSON), so we keep a small process-local
# registry. The reviewer registers their agent on startup; the API resolves
# names to instances on each request.

_adapter_registry: dict[str, AgentAdapter] = {"echo": echo_adapter()}


def register_adapter(name: str, adapter: AgentAdapter) -> None:
    """Make a named adapter available to ``POST /api/eval/runs``."""
    _adapter_registry[name] = adapter


def unregister_adapter(name: str) -> None:
    _adapter_registry.pop(name, None)


def list_adapter_names() -> list[str]:
    return sorted(_adapter_registry)


def get_adapter(name: str) -> AgentAdapter | None:
    """Return the named adapter, or ``None`` if not registered.

    Public counterpart to the module-private ``_adapter_registry`` —
    callers outside this file should go through this function rather
    than poking at the dict directly.
    """
    return _adapter_registry.get(name)


def _resolve_adapter(name: str) -> AgentAdapter:
    if name not in _adapter_registry:
        raise HTTPException(
            400,
            f"Unknown adapter {name!r}. Known: {list_adapter_names()}. "
            "Register one via butterfly.eval_engine.api.register_adapter()."
        )
    return _adapter_registry[name]


# ── Router factory ────────────────────────────────────────────────────────────

def create_router(service: EvalService | None = None) -> APIRouter:
    svc = service or get_default_service()
    router = APIRouter(prefix="/api/eval", tags=["eval"])

    @router.get("/benchmarks")
    async def list_benchmarks() -> dict:
        """Catalog of registered benchmarks."""
        return {"benchmarks": svc.list_benchmarks()}

    @router.get("/adapters")
    async def list_adapters() -> dict:
        """Names of agent adapters available to ``POST /runs``."""
        return {"adapters": list_adapter_names()}

    @router.get("/runs")
    async def list_runs() -> dict:
        return {"runs": svc.list_runs()}

    @router.post("/runs")
    async def create_run(body: dict[str, Any]) -> dict:
        """Schedule a new run.

        Body:
          benchmark   (str, required) — benchmark id from /benchmarks
          adapter     (str, optional) — adapter name (default: "echo")
          limit       (int, optional) — cap on tasks (default: benchmark default)
          parallel    (int, optional) — concurrent in-flight tasks (default: 1)
          adapter_config (obj, optional) — passed to the benchmark factory
                       (e.g. ``{"mode": "smoke"}`` or ``{"domain": "retail"}``)
          sync        (bool, optional) — when true, block until the run
                       finishes and return the final EvalRun. Defaults to
                       false (fire-and-forget).
        """
        bench_id = body.get("benchmark")
        if not bench_id:
            raise HTTPException(400, "body.benchmark is required")
        adapter_name = body.get("adapter", "echo")
        adapter = _resolve_adapter(adapter_name)
        limit = body.get("limit")
        parallel = int(body.get("parallel", 1))
        adapter_config = body.get("adapter_config") or {}
        sync = bool(body.get("sync", False))
        if not isinstance(adapter_config, dict):
            raise HTTPException(400, "body.adapter_config must be an object")

        try:
            if sync:
                run = await svc.run_benchmark(
                    bench_id,
                    adapter=adapter,
                    limit=limit,
                    parallel=parallel,
                    adapter_config=adapter_config,
                    agent_label=adapter_name,
                )
                return run.to_dict()
            return svc.submit_run(
                bench_id,
                adapter=adapter,
                limit=limit,
                parallel=parallel,
                adapter_config=adapter_config,
                agent_label=adapter_name,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict:
        run = svc.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"Unknown run_id: {run_id!r}")
        return run

    @router.get("/runs/{run_id}/results")
    async def get_results(run_id: str) -> dict:
        run = svc.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"Unknown run_id: {run_id!r}")
        return {"run_id": run_id, "results": svc.read_results(run_id)}

    @router.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict:
        run = svc.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"Unknown run_id: {run_id!r}")
        ok = svc.cancel_run(run_id)
        return {"run_id": run_id, "cancelling": ok}

    @router.delete("/runs/{run_id}")
    async def delete_run(run_id: str) -> dict:
        try:
            ok = svc.delete_run(run_id)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not ok:
            raise HTTPException(404, f"Unknown run_id: {run_id!r}")
        return {"run_id": run_id, "deleted": True}

    return router
