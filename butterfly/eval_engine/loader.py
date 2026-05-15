"""EvalLoader — discovers eval plugins from ``evalhub/`` and an ``evals.md``.

Mirrors ``tool_engine.loader`` / ``skill_engine.loader``:

  evalhub/<name>/
    eval.json        # static descriptor (BenchmarkInfo)
    adapter.py       # defines `class Adapter(Benchmark)`

  evals.md           # one enabled eval name per line (# comments + blanks ok)

Discovery order:

1. ``evals.md`` lists which evals are enabled (whitelist).
2. For each name, read ``evalhub/<name>/eval.json`` → :class:`BenchmarkInfo`.
3. Dynamically import ``evalhub/<name>/adapter.py``; instantiate
   ``Adapter(info, **adapter_config)``.

The loader is intentionally side-effect free: it returns adapter
instances on demand, and never registers globally. The
``registry.py`` shim still exists for in-process registration (tests
and external integrations); the production path is loader-only.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from butterfly.eval_engine.benchmark import Benchmark
from butterfly.eval_engine.types import BenchmarkInfo


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_EVALHUB_DIR = _REPO_ROOT / "evalhub"
_DEFAULT_EVALS_MD = _REPO_ROOT / "evals.md"


def _read_evals_md(path: Path) -> list[str]:
    """Read evals.md and return list of eval names.

    Same shape as ``tools.md`` / ``skills.md``: one name per line,
    blank lines and ``#``-comments are ignored.
    """
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _load_eval_info(name: str, evalhub_dir: Path) -> BenchmarkInfo | None:
    schema = evalhub_dir / name / "eval.json"
    if not schema.is_file():
        return None
    try:
        data = json.loads(schema.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # Tolerate unknown JSON keys for forward-compat (mirrors what
    # store.py does for EvalRun).
    from dataclasses import fields
    keep = {f.name for f in fields(BenchmarkInfo)}
    return BenchmarkInfo(**{k: v for k, v in data.items() if k in keep})


def _load_adapter_module(name: str, evalhub_dir: Path):
    adapter_path = evalhub_dir / name / "adapter.py"
    if not adapter_path.is_file():
        return None
    mod_name = f"evalhub_{name.replace('-', '_')}_adapter"
    spec = importlib.util.spec_from_file_location(mod_name, adapter_path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    # We register the freshly-built module under ``mod_name`` in
    # ``sys.modules`` so dataclass / pickle / typing-introspection paths
    # can resolve names back to it. Re-loading the same plugin name
    # against a different ``evalhub_dir`` (e.g. successive tests with
    # tmp_path) overwrites the slot and ``exec_module`` re-runs the body
    # against the new source — deliberate, so per-test isolation works.
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class EvalLoader:
    """Load eval adapters from ``evalhub/`` + ``evals.md``.

    Args:
        evalhub_dir: override the evalhub directory (testing).
        evals_md_path: override the evals.md path. Defaults to
            ``<repo_root>/evals.md``. Pass a different path to load a
            per-agent or per-suite manifest.
    """

    def __init__(
        self,
        evalhub_dir: Path | None = None,
        evals_md_path: Path | None = None,
    ) -> None:
        self._evalhub_dir = evalhub_dir or _EVALHUB_DIR
        self._evals_md = evals_md_path or _DEFAULT_EVALS_MD

    # ── Discovery ───────────────────────────────────────────────────────

    def list_available(self) -> list[BenchmarkInfo]:
        """Every adapter present in ``evalhub/``, regardless of evals.md."""
        if not self._evalhub_dir.is_dir():
            return []
        out: list[BenchmarkInfo] = []
        for d in sorted(self._evalhub_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            info = _load_eval_info(d.name, self._evalhub_dir)
            if info is not None:
                out.append(info)
        return out

    def list_enabled(self, evals_md_path: Path | None = None) -> list[str]:
        """Names declared in ``evals.md`` (the whitelist)."""
        return _read_evals_md(evals_md_path or self._evals_md)

    def get_info(self, name: str) -> BenchmarkInfo | None:
        return _load_eval_info(name, self._evalhub_dir)

    # ── Instantiation ───────────────────────────────────────────────────

    def load(self, name: str, **adapter_config: Any) -> Benchmark:
        """Load + instantiate one adapter by name.

        Raises ``KeyError`` if the plugin isn't found in evalhub,
        ``ImportError`` if its ``adapter.py`` doesn't expose ``Adapter``.
        Extra kwargs flow into ``Adapter.__init__`` after ``info``.
        """
        info = _load_eval_info(name, self._evalhub_dir)
        if info is None:
            raise KeyError(f"No eval {name!r} in {self._evalhub_dir}")
        mod = _load_adapter_module(name, self._evalhub_dir)
        if mod is None:
            raise ImportError(f"evalhub/{name}/adapter.py not found or invalid")
        cls = getattr(mod, "Adapter", None)
        if cls is None:
            raise ImportError(
                f"evalhub/{name}/adapter.py must define `class Adapter(Benchmark)`"
            )
        return cls(info, **adapter_config)

    def load_enabled(
        self,
        evals_md_path: Path | None = None,
        adapter_configs: dict[str, dict] | None = None,
    ) -> list[Benchmark]:
        """Instantiate every adapter listed in ``evals.md``.

        ``adapter_configs`` is a per-eval-name dict of kwargs forwarded
        to that adapter's constructor (e.g. ``{"tau-bench": {"mode":
        "smoke", "domain": "retail"}}``).
        """
        names = self.list_enabled(evals_md_path)
        out: list[Benchmark] = []
        cfgs = adapter_configs or {}
        for name in names:
            try:
                out.append(self.load(name, **cfgs.get(name, {})))
            except (KeyError, ImportError) as exc:
                print(
                    f"[eval_engine] Warning: skipping {name!r}: {exc}",
                    file=sys.stderr,
                )
        return out
