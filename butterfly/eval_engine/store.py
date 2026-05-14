"""On-disk persistence for eval runs.

Layout (mirrors the sessions/ → _sessions/ split):

    <root>/                    # default: <repo_root>/_evals
      <run_id>/
        run.json               # EvalRun (serialised)
        results.jsonl          # one TaskResult per line, append-only

The runner only needs three operations: ``create``, ``append_result``,
``finalize``. The service adds ``get``, ``list``, ``read_results``.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Iterator

from butterfly.eval_engine.types import EvalRun, TaskResult


_SAFE_ID = re.compile(r"^[\w\-]+$")


class EvalStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        # Append/finalize across threads must not interleave run.json writes.
        self._lock = threading.Lock()

    @staticmethod
    def _validate(run_id: str) -> None:
        if not _SAFE_ID.match(run_id):
            raise ValueError(f"Invalid run_id: {run_id!r}")

    def run_dir(self, run_id: str) -> Path:
        self._validate(run_id)
        return self.root / run_id

    def create(self, run: EvalRun) -> None:
        d = self.run_dir(run.run_id)
        d.mkdir(parents=True, exist_ok=True)
        self._write_run(run)
        (d / "results.jsonl").touch(exist_ok=True)

    def update(self, run: EvalRun) -> None:
        with self._lock:
            self._write_run(run)

    def _write_run(self, run: EvalRun) -> None:
        d = self.run_dir(run.run_id)
        tmp = d / "run.json.tmp"
        tmp.write_text(json.dumps(run.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(d / "run.json")

    def get(self, run_id: str) -> EvalRun | None:
        d = self.run_dir(run_id)
        path = d / "run.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        # Hand-roll the round-trip — dataclasses.fields lets us drop
        # unknown keys (forward-compat) without pulling pydantic in.
        from dataclasses import fields
        from butterfly.eval_engine.types import RunSummary
        summary_fields = {f.name for f in fields(RunSummary)}
        run_fields = {f.name for f in fields(EvalRun)}
        summary_data = {
            k: v for k, v in data.pop("summary", {}).items() if k in summary_fields
        }
        run_data = {k: v for k, v in data.items() if k in run_fields}
        return EvalRun(summary=RunSummary(**summary_data), **run_data)

    def list(self) -> list[EvalRun]:
        out: list[EvalRun] = []
        if not self.root.is_dir():
            return out
        for d in sorted(self.root.iterdir()):
            if not d.is_dir():
                continue
            run = self.get(d.name)
            if run is not None:
                out.append(run)
        # Newest-first.
        out.sort(key=lambda r: r.created_at, reverse=True)
        return out

    def append_result(self, run_id: str, result: TaskResult) -> None:
        d = self.run_dir(run_id)
        if not d.is_dir():
            raise FileNotFoundError(f"Unknown run_id: {run_id!r}")
        with self._lock:
            with (d / "results.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(result.to_dict()) + "\n")

    def read_results(self, run_id: str) -> Iterator[dict]:
        d = self.run_dir(run_id)
        path = d / "results.jsonl"
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue

    def delete(self, run_id: str) -> bool:
        d = self.run_dir(run_id)
        if not d.is_dir():
            return False
        # Belt-and-braces: make sure the path is under root before rm-rfing.
        if self.root.resolve() not in d.resolve().parents:
            raise RuntimeError(f"Refusing to delete outside root: {d}")
        for sub in d.iterdir():
            sub.unlink(missing_ok=True)
        d.rmdir()
        return True
