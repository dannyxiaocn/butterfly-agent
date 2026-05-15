"""``butterfly eval`` CLI surface.

Subcommands:

* ``butterfly eval list``  — show every plugin under ``evalhub/`` and
  which ones the repo-level ``evals.md`` currently enables.
* ``butterfly eval run``   — run one or more enabled benchmarks against
  an adapter. ``--enable a,b,c`` selects which benchmarks; without it
  every entry in ``evals.md`` is used.

The CLI is intentionally thin — every subcommand wires its arguments
into :class:`EvalService` so the same code path also serves the
``/api/eval/...`` HTTP surface.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Iterable

from butterfly.eval_engine.agent_adapter import (
    AgentAdapter,
    CallableAdapter,
)
from butterfly.eval_engine.loader import EvalLoader
from butterfly.eval_engine.service import EvalService, echo_adapter


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_EVALS_MD = _REPO_ROOT / "evals.md"
_DEFAULT_EVALS_ROOT = _REPO_ROOT / "_evals"


# ── Adapter resolution ──────────────────────────────────────────────────────

def _resolve_adapter(name: str) -> AgentAdapter:
    """Map a CLI ``--adapter`` string to an :class:`AgentAdapter`.

    Built-ins:

    * ``echo`` — no-op (default). Useful for confirming wiring.
    * ``mock-passing`` — returns whatever ``task.metadata['expected']``
      contains; pairs with the smoke tasks for a sanity-check pass run.

    Anything else is treated as an entry registered via
    :func:`butterfly.eval_engine.api.register_adapter` (the HTTP-layer
    process-local table). This lets a developer plug in their real
    agent once and use it from both CLI and HTTP.
    """
    if name == "echo":
        return echo_adapter()
    if name == "mock-passing":
        # Every smoke task in evalhub/ ships an ``expected_output`` field
        # — the reference solution. The mock just echoes it so the CLI
        # wiring can be sanity-checked across all built-in benchmarks
        # (PR #72 review item 1).
        return CallableAdapter(
            lambda task: str(task.metadata.get("expected_output", "")),
            name="mock-passing",
        )
    from butterfly.eval_engine.api import get_adapter, list_adapter_names
    adapter = get_adapter(name)
    if adapter is not None:
        return adapter
    raise SystemExit(
        f"butterfly eval: unknown adapter {name!r}. Known: echo, mock-passing, "
        f"{sorted(set(list_adapter_names()) - {'echo'})}. Register one via "
        "butterfly.eval_engine.api.register_adapter()."
    )


def _resolve_enable(enable_arg: str | None, loader: EvalLoader) -> list[str]:
    if enable_arg:
        names = [n.strip() for n in enable_arg.split(",") if n.strip()]
    else:
        # Loader was constructed with the evals.md path; reuse it.
        names = loader.list_enabled()
    if not names:
        raise SystemExit(
            "butterfly eval: nothing enabled. Either pass --enable a,b,c "
            f"or list benchmarks in {loader._evals_md}."  # noqa: SLF001
        )
    available = {info.id for info in loader.list_available()}
    missing = [n for n in names if n not in available]
    if missing:
        raise SystemExit(
            f"butterfly eval: unknown benchmarks {missing}. Available: "
            f"{sorted(available)}"
        )
    return names


# ── Sub-commands ────────────────────────────────────────────────────────────

def cmd_list(args) -> int:
    loader = EvalLoader(
        evalhub_dir=Path(args.evalhub_dir) if args.evalhub_dir else None,
        evals_md_path=Path(args.evals_md) if args.evals_md else None,
    )
    available = loader.list_available()
    enabled = set(loader.list_enabled())
    if args.json:
        payload = [
            {**info.to_dict(), "enabled": info.id in enabled}
            for info in available
        ]
        print(json.dumps(payload, indent=2))
        return 0
    if not available:
        print(
            f"(no evalhub plugins found under "
            f"{loader._evalhub_dir})",  # noqa: SLF001 - reading state
            file=sys.stderr,
        )
        return 1
    print(f"{'ENABLED':<8} {'ID':<22} {'METRIC':<16} {'NAME'}")
    for info in available:
        mark = "•" if info.id in enabled else " "
        print(f"   {mark}     {info.id:<22} {info.metric:<16} {info.name}")
    return 0


def cmd_run(args) -> int:
    loader = EvalLoader(
        evalhub_dir=Path(args.evalhub_dir) if args.evalhub_dir else None,
        evals_md_path=Path(args.evals_md) if args.evals_md else None,
    )
    names = _resolve_enable(args.enable, loader)
    adapter = _resolve_adapter(args.adapter)
    root = Path(args.evals_root) if args.evals_root else _DEFAULT_EVALS_ROOT
    svc = EvalService(root)

    # Point registry.get at the loader for this run, in case the
    # adapter_config contains keys that the plugin's ``Adapter.__init__``
    # accepts (mode, domain, timeout_s, ...).
    from butterfly.eval_engine import registry
    registry.set_loader(loader)

    adapter_config: dict = {}
    if args.mode:
        adapter_config["mode"] = args.mode
    if args.adapter_config:
        try:
            extra = json.loads(args.adapter_config)
        except ValueError as exc:
            raise SystemExit(
                f"butterfly eval: --adapter-config must be valid JSON ({exc})"
            )
        if not isinstance(extra, dict):
            raise SystemExit("--adapter-config must be a JSON object")
        adapter_config.update(extra)

    async def _run_all() -> int:
        rc = 0
        for name in names:
            print(f"\n▶ {name}", flush=True)
            run = await svc.run_benchmark(
                name,
                adapter=adapter,
                limit=args.limit,
                parallel=args.parallel,
                adapter_config=dict(adapter_config),
                agent_label=args.adapter,
            )
            s = run.summary
            print(
                f"  {run.status}: passed={s.passed}/{s.total} "
                f"failed={s.failed} errored={s.errored} "
                f"skipped={s.skipped} pass_rate={s.pass_rate:.0%}"
            )
            print(f"  run_id={run.run_id}  artifacts={root / run.run_id}")
            if args.json:
                print(json.dumps(run.to_dict(), indent=2))
            if s.errored or (run.status == "failed"):
                rc = 1
        return rc

    return asyncio.run(_run_all())


# ── argparse wiring ─────────────────────────────────────────────────────────

def _add_common_args(p) -> None:
    p.add_argument(
        "--evalhub-dir", metavar="DIR",
        help="Override evalhub root (default: <repo>/evalhub)",
    )
    p.add_argument(
        "--evals-md", metavar="PATH",
        help="Override evals.md manifest (default: <repo>/evals.md)",
    )


def add_eval_parser(subparsers) -> None:
    """Wire ``butterfly eval ...`` into the top-level CLI parser."""
    p = subparsers.add_parser(
        "eval",
        help="Run agent evaluation benchmarks (evalhub).",
        description=(
            "Run agent evaluation benchmarks shipped in `evalhub/` and "
            "selected via `evals.md`.\n\nExamples:\n"
            "  butterfly eval list\n"
            "  butterfly eval run --enable tau-bench --adapter mock-passing\n"
            "  butterfly eval run --enable swe-bench-verified,terminal-bench \\\n"
            "      --limit 3 --mode smoke\n"
            "  butterfly eval run                # use everything in evals.md\n"
        ),
    )
    sub = p.add_subparsers(dest="eval_cmd", metavar="COMMAND")
    sub.required = True

    plist = sub.add_parser("list", help="List available + enabled benchmarks.")
    _add_common_args(plist)
    plist.add_argument("--json", action="store_true",
                       help="Emit machine-readable JSON.")
    plist.set_defaults(func=cmd_list)

    prun = sub.add_parser("run", help="Execute one or more benchmarks.")
    _add_common_args(prun)
    prun.add_argument(
        "--enable", metavar="NAMES",
        help="Comma-separated benchmark ids to run. Defaults to evals.md.",
    )
    prun.add_argument(
        "--adapter", default="echo", metavar="NAME",
        help="Agent adapter (default: echo). Use mock-passing for a smoke "
             "sanity check, or register your own via "
             "butterfly.eval_engine.api.register_adapter().",
    )
    prun.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Cap tasks per benchmark (default: each benchmark's own default).",
    )
    prun.add_argument(
        "--parallel", type=int, default=1, metavar="N",
        help="Concurrent in-flight tasks per benchmark (default: 1).",
    )
    prun.add_argument(
        "--mode", choices=("auto", "smoke", "upstream"), default=None,
        help="Force the adapter mode for every benchmark in this run.",
    )
    prun.add_argument(
        "--adapter-config", metavar="JSON",
        help="Extra JSON dict forwarded to each adapter (e.g. '{\"domain\": "
             "\"retail\"}'). Merges on top of --mode.",
    )
    prun.add_argument(
        "--evals-root", metavar="DIR",
        help="Where to persist runs (default: <repo>/_evals).",
    )
    prun.add_argument("--json", action="store_true",
                      help="Emit the full EvalRun JSON after each benchmark.")
    prun.set_defaults(func=cmd_run)
